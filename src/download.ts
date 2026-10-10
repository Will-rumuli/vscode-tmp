/* 统一下载策略: 代理 + 镜像加速。
 *
 * 项目里的下载点分散在两层, 这里负责把用户配置解析成**一份**策略, 再翻译给两层:
 *   1. 扩展本机 (本文件): TS 侧直接用 —— 代理感知的 HTTP 下载、pip 的代理环境变量。
 *   2. SDK 主机 (Python 经 shell 执行): 由 backend.ts 把策略作为 QPI_DL_PROXY /
 *      QPI_DL_MIRROR 注入后端进程, 后端拼进每条下载命令的环境变量。
 *
 * 约束: 项目零运行时依赖 (package.json 只有 devDependencies, vsce 用
 * --no-dependencies), 所以不能引 https-proxy-agent / socks-proxy-agent。
 * 因此: 未配代理时用 Node 原生 http/https (保留逐字节进度与完整性校验);
 * 配置了代理时改用系统 curl (-x) —— curl 原生支持 http/https/socks 代理、
 * 重定向与断点续传, 比手写 CONNECT + SOCKS 隧道可靠得多。
 */
import * as vscode from 'vscode';
import * as fs from 'fs';
import * as http from 'http';
import * as https from 'https';
import { spawn } from 'child_process';
import { t } from './i18n';
import * as output from './output';

export type MirrorMode = 'auto' | 'cn' | 'global' | 'off';

export interface DownloadSettings {
  /** 规范化后的代理地址, '' 表示直连 */
  proxy: string;
  /** 镜像加速策略 */
  mirror: MirrorMode;
}

/* ---------------- 镜像源 ---------------- */

/** 内置 Python 运行时下载前缀 (不含版本 tag) */
const PY_MIRROR_CN = 'https://mirror.nju.edu.cn/github-release/astral-sh/python-build-standalone';
const PY_MIRROR_GLOBAL = 'https://github.com/astral-sh/python-build-standalone/releases/download';
const PIP_MIRROR_CN = 'https://pypi.tuna.tsinghua.edu.cn/simple';
const PIP_MIRROR_GLOBAL = 'https://pypi.org/simple';

/** 国内镜像 / 直连白名单: 这些主机即使开了代理也直连 (对应 shell 侧的 no_proxy)。
 *  国内镜像本来就比绕代理快, 因此"代理 + 镜像"是互补而不是二选一。 */
export const DIRECT_HOSTS = [
  'mirrors.tuna.tsinghua.edu.cn',
  'mirrors.ustc.edu.cn',
  'mirrors.aliyun.com',
  'mirror.nju.edu.cn',
  'pypi.tuna.tsinghua.edu.cn',
];

/** 用于判断"是否在国内网络"的探测端点 (任一可达即判定为国内) */
const CN_PROBE_HOSTS = ['https://pypi.tuna.tsinghua.edu.cn', 'https://mirror.nju.edu.cn'];

/* ---------------- 配置读取 ---------------- */

/** 规范化代理地址: 允许用户只写 host:port; 空/非法一律返回 '' (直连) */
export function normalizeProxy(raw: string): string {
  const s = (raw || '').trim();
  if (!s) { return ''; }
  if (/^[a-z][a-z0-9+.-]*:\/\//i.test(s)) { return s; }
  return `http://${s}`;
}

export function downloadSettings(): DownloadSettings {
  const cfg = vscode.workspace.getConfiguration('qpiSdk');
  const mirror = (cfg.get<string>('download.mirror') || 'auto') as MirrorMode;
  return {
    proxy: normalizeProxy(cfg.get<string>('download.proxy') || ''),
    mirror: (['auto', 'cn', 'global', 'off'] as const).includes(mirror) ? mirror : 'auto',
  };
}

/* ---------------- 环境变量翻译 ---------------- */

/** 判断某个主机名是否应直连 (国内镜像 / 本地回环) */
export function hostIsDirect(host: string): boolean {
  const h = host.toLowerCase();
  if (host === '') { return true; }
  if (h === 'localhost' || h === '127.0.0.1' || h === '::1') { return true; }
  return DIRECT_HOSTS.some((d) => h === d);
}

/** 把代理策略翻译成代理环境变量 (http_proxy/https_proxy/ALL_PROXY/no_proxy)。
 *  curl / wget / git / pip / apt 都认这几个变量; subst 传入现有环境则在其上叠加。
 *  未配代理返回 {}. */
export function proxyEnv(subst: NodeJS.ProcessEnv = {}): NodeJS.ProcessEnv {
  const { proxy } = downloadSettings();
  if (!proxy) { return {}; }
  const noProxy = [...DIRECT_HOSTS, 'localhost', '127.0.0.1'].join(',');
  return {
    http_proxy: proxy,
    https_proxy: proxy,
    HTTP_PROXY: proxy,
    HTTPS_PROXY: proxy,
    ALL_PROXY: proxy,
    all_proxy: proxy,
    no_proxy: noProxy,
    NO_PROXY: noProxy,
    ...subst,
  };
}

/** 把策略翻译成给 Python 后端用的环境变量 (后端拼进 shell 下载命令) */
export function backendDownloadEnv(): Record<string, string> {
  const { proxy, mirror } = downloadSettings();
  const env: Record<string, string> = {
    QPI_DL_PROXY: proxy,
    QPI_DL_MIRROR: mirror,
  };
  // 配了代理: 走代理即可, 不再叠 GitHub 加速前缀 (置空让 docker_setup 选直连);
  // 未配代理时不设置该变量, 后端继承用户环境或使用内置加速前缀默认值。
  if (proxy) { env.QPI_GH_PROXY = ''; }
  return env;
}

/* ---------------- 网络环境与镜像顺序 ---------------- */

function reachable(url: string, timeoutMs: number): Promise<boolean> {
  return new Promise((resolve) => {
    const req = https.get(url, { timeout: timeoutMs }, (res) => {
      res.resume();
      resolve(true);
    });
    req.on('timeout', () => { req.destroy(); resolve(false); });
    req.on('error', () => resolve(false));
  });
}

/** 判断当前网络环境: 国内端点可达返回 'cn', 否则 'global' (并行探测, 最多 ~1.5s) */
export async function detectRegion(): Promise<'cn' | 'global'> {
  const results = await Promise.all(CN_PROBE_HOSTS.map((u) => reachable(u, 1500)));
  return results.some(Boolean) ? 'cn' : 'global';
}

/** 按策略给出候选镜像顺序 (依次尝试, 前一个失败换下一个) */
export async function mirrorOrder(cn: string, global: string): Promise<string[]> {
  const { mirror } = downloadSettings();
  if (mirror === 'off') { return [global]; }
  if (mirror === 'cn') { return [cn, global]; }
  if (mirror === 'global') { return [global, cn]; }
  const region = await detectRegion();
  return region === 'cn' ? [cn, global] : [global, cn];
}

/** Python 运行时下载前缀候选 (用户可在 backend.pythonMirror 显式覆盖) */
export async function pythonMirrorOrder(): Promise<string[]> {
  const configured = (vscode.workspace.getConfiguration('qpiSdk')
    .get<string>('backend.pythonMirror') || '').trim();
  if (configured) { return [configured.replace(/\/+$/, '')]; }
  return mirrorOrder(PY_MIRROR_CN, PY_MIRROR_GLOBAL);
}

/** pip 依赖下载源候选 (用户可在 backend.pipMirror 显式覆盖) */
export async function pipMirrorOrder(): Promise<string[]> {
  const configured = (vscode.workspace.getConfiguration('qpiSdk')
    .get<string>('backend.pipMirror') || '').trim();
  if (configured) { return [configured.replace(/\/+$/, '')]; }
  return mirrorOrder(PIP_MIRROR_CN, PIP_MIRROR_GLOBAL);
}

/* ---------------- 下载实现 ---------------- */

export interface DownloadResult { got: number; total: number; }

export interface DownloadOpts {
  progress?: vscode.Progress<{ message?: string; increment?: number }>;
  /** true 时按 10% 步进写入输出面板 (只对首个候选源记录, 避免重试刷屏) */
  logProgress?: boolean;
  /** 10% 步进回调 (供调用方本地化文案) */
  onPct?: (pct: number, got: number, total: number) => void;
}

/** 下载文件到 dest。未配代理走 Node 原生 (逐字节进度 + 完整 size 校验);
 *  配了代理走系统 curl (支持 http/https/socks 代理, 此时 total 返回 0)。 */
export async function downloadFile(
  url: string,
  dest: string,
  opts: DownloadOpts = {},
): Promise<DownloadResult> {
  const { proxy } = downloadSettings();
  const target = new URL(url);
  if (proxy && !hostIsDirect(target.hostname)) {
    return downloadViaCurl(url, dest, proxy);
  }
  return downloadNative(url, dest, opts);
}

/** Node 原生下载: 支持 HTTP 重定向, 向 withProgress 报告增量,
 *  logProgress=true 时另外按 10% 步进写输出面板。 */
export function downloadNative(
  url: string,
  dest: string,
  opts: DownloadOpts = {},
): Promise<DownloadResult> {
  const { progress, logProgress = false, onPct } = opts;
  return new Promise((resolve, reject) => {
    const file = fs.createWriteStream(dest);
    const mod = url.startsWith('https:') ? https : http;
    const req = mod.get(url, (res) => {
      const status = res.statusCode ?? 0;
      const loc = res.headers.location;
      if (status >= 300 && status < 400 && loc) {
        res.resume();
        file.destroy();
        downloadNative(loc, dest, opts).then(resolve, reject);
        return;
      }
      if (status !== 200) {
        res.resume();
        file.destroy();
        reject(new Error(`HTTP ${status}`));
        return;
      }
      const total = parseInt(res.headers['content-length'] || '0', 10) || 0;
      let got = 0;
      let lastMark = -1;
      res.on('data', (chunk: Buffer) => {
        got += chunk.length;
        if (total > 0 && progress) {
          progress.report({ increment: (chunk.length / total) * 100 });
        }
        if (total > 0) {
          const pct = Math.floor((got / total) * 100 / 10) * 10;
          if (pct > lastMark) {
            lastMark = pct;
            if (logProgress && onPct) { onPct(pct, got, total); }
          }
        }
      });
      res.pipe(file);
      file.on('finish', () => file.close(() => resolve({ got, total })));
      file.on('error', (e) => {
        res.destroy();
        reject(e);
      });
    });
    req.on('error', (e) => {
      file.destroy();
      reject(e);
    });
  });
}

/** 经系统 curl 下载 (走代理)。curl 原生支持 http/https/socks 代理与断点续传,
 *  但拿不到 Content-Length, 故 total 返回 0 (完整性由 gzip 魔数 + tar 兜底)。 */
function downloadViaCurl(url: string, dest: string, proxy: string): Promise<DownloadResult> {
  return new Promise((resolve, reject) => {
    output.log(t('download.logViaProxy', { proxy }));
    const args = [
      '-fL', '--retry', '2', '--connect-timeout', '20',
      '--speed-limit', '1024', '--speed-time', '30',
      '-x', proxy, '-o', dest,
      '-w', '%{size_download}',
      url,
    ];
    const p = spawn('curl', args, { stdio: ['ignore', 'pipe', 'pipe'] });
    let out = '';
    let err = '';
    p.stdout?.on('data', (d: Buffer) => { out += d.toString(); });
    p.stderr?.on('data', (d: Buffer) => { err += d.toString(); });
    p.on('error', (e) => reject(new Error(t('download.curlMissing', { err: e.message }))));
    p.on('close', (code) => {
      if (code !== 0) {
        const tail = err.trim().split(/\r?\n/).slice(-3).join(' | ');
        reject(new Error(t('download.curlFailed', { code: String(code), err: tail || 'unknown' })));
        return;
      }
      const got = parseInt(out.trim(), 10) || 0;
      output.log(t('download.logViaProxyDone', { got: String(got) }));
      resolve({ got, total: 0 });
    });
  });
}

/* ---------------- 配置命令 (QuickPick) ---------------- */

const MIRROR_LABEL_KEY: Record<MirrorMode, string> = {
  auto: 'download.cfg.mirrorAuto',
  cn: 'download.cfg.mirrorCn',
  global: 'download.cfg.mirrorGlobal',
  off: 'download.cfg.mirrorOff',
};

function mirrorLabel(mode: MirrorMode): string {
  return t(MIRROR_LABEL_KEY[mode]);
}

/** 命令入口: 让用户选择是否走代理 (及地址) 与镜像加速策略。
 *  写入全局配置 (user settings); 返回是否发生修改, 调用方据此提示重启后端。 */
export async function configureDownloadCommand(): Promise<boolean> {
  const cfg = vscode.workspace.getConfiguration('qpiSdk');
  const cur = downloadSettings();
  const summary = cur.proxy
    ? t('download.cfg.summaryProxy', { proxy: cur.proxy })
    : t('download.cfg.summaryDirect');
  const pick = await vscode.window.showQuickPick<vscode.QuickPickItem & { id: string }>(
    [
      {
        id: 'proxy',
        label: cur.proxy ? t('download.cfg.changeProxy') : t('download.cfg.setProxy'),
        description: t('download.cfg.setProxyDesc'),
      },
      {
        id: 'direct',
        label: t('download.cfg.direct'),
        description: t('download.cfg.directDesc'),
      },
      {
        id: 'mirror',
        label: t('download.cfg.mirror', { mode: mirrorLabel(cur.mirror) }),
        description: t('download.cfg.mirrorDesc'),
      },
    ],
    { placeHolder: t('download.cfg.placeholder', { summary }) },
  );
  if (!pick) { return false; }

  if (pick.id === 'proxy') {
    const v = await vscode.window.showInputBox({
      prompt: t('download.cfg.proxyPrompt'),
      value: cur.proxy,
      placeHolder: 'http://127.0.0.1:7890',
      ignoreFocusOut: true,
    });
    if (v === undefined) { return false; } // 用户取消
    const next = normalizeProxy(v);
    await cfg.update('download.proxy', next, vscode.ConfigurationTarget.Global);
    vscode.window.showInformationMessage(t('download.cfg.saved'));
    return next !== cur.proxy;
  }

  if (pick.id === 'direct') {
    if (!cur.proxy) { return false; }
    await cfg.update('download.proxy', '', vscode.ConfigurationTarget.Global);
    vscode.window.showInformationMessage(t('download.cfg.saved'));
    return true;
  }

  // 镜像策略
  const m = await vscode.window.showQuickPick<vscode.QuickPickItem & { id: MirrorMode }>(
    (['auto', 'cn', 'global', 'off'] as MirrorMode[]).map((mode) => ({ id: mode, label: mirrorLabel(mode) })),
    { placeHolder: t('download.cfg.mirrorPlaceholder') },
  );
  if (!m) { return false; }
  await cfg.update('download.mirror', m.id, vscode.ConfigurationTarget.Global);
  vscode.window.showInformationMessage(t('download.cfg.saved'));
  return m.id !== cur.mirror;
}