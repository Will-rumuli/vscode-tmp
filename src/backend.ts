/* Python 后端进程管理: 生命周期、venv 自动创建、READY 协议握手 */
import * as vscode from 'vscode';
import * as fs from 'fs';
import * as path from 'path';
import { ChildProcess, spawn } from 'child_process';
import { t, currentLanguage } from './i18n';
import * as output from './output';
import {
  downloadFile,
  pythonMirrorOrder,
  pipMirrorOrder,
  proxyEnv,
  backendDownloadEnv,
} from './download';

/** 自动下载的便携 Python 运行时: python-build-standalone install_only 版 (含 pip/venv, 解压即用)。
 *  版本升级时同步更新 PY_BUILD_VER / PY_BUILD_TAG (tag 为 release 目录名)。 */
const PY_BUILD_VER = '3.12.13';
const PY_BUILD_TAG = '20260623';

/** 跨窗口环境初始化互斥: 心跳刷新间隔、判定锁陈旧的阈值、持锁者存活时的强制夺锁上限 */
const LOCK_HEARTBEAT_MS = 5000;
const LOCK_STALE_MS = 30000;
const LOCK_MAX_WAIT_MS = 10 * 60 * 1000;

/** 当前平台对应的 python-build-standalone 目标标识, 不支持的平台返回 null */
function pbsTarget(): string | null {
  const arch =
    process.arch === 'x64' ? 'x86_64' : process.arch === 'arm64' ? 'aarch64' : '';
  if (!arch) {
    return null;
  }
  const os =
    process.platform === 'win32'
      ? 'pc-windows-msvc'
      : process.platform === 'linux'
        ? 'unknown-linux-gnu'
        : process.platform === 'darwin'
          ? 'apple-darwin'
          : '';
  return os ? `${arch}-${os}` : null;
}

function pbsFile(target: string): string {
  return `cpython-${PY_BUILD_VER}+${PY_BUILD_TAG}-${target}-install_only_stripped.tar.gz`;
}

/** 任意抛错转简短可读文本 (供日志使用, 不吞掉 Error 的 message) */
function errMsg(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

/** 字节数转人类可读 (下载进度日志用) */
function fmtBytes(n: number): string {
  if (n < 1024) { return `${n}B`; }
  const units = ['KB', 'MB', 'GB'];
  let v = n;
  let i = -1;
  do { v /= 1024; i++; } while (v >= 1024 && i < units.length - 1);
  return `${v.toFixed(1)}${units[i]}`;
}

/** 简单延时 (锁重试退避用) */
function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export interface BackendHandle {
  port: number;
  key: string;
  host: string;
  process: ChildProcess | null;
}

export class BackendManager {
  private handle: BackendHandle | null = null;
  /** 启动中 Promise: 防止 activate 自动启动与用户操作并发初始化 */
  private starting: Promise<BackendHandle> | null = null;
  /** 启动中等待 READY 的后端进程 (供 stop 中途取消) */
  private pendingProc: ChildProcess | null = null;
  /** 环境准备锁的心跳定时器 (持有期间定时刷新锁文件 mtime) */
  private heartbeat: NodeJS.Timeout | null = null;
  private readonly extRoot: string;
  private readonly globalStorage: string;

  constructor(context: vscode.ExtensionContext) {
    this.extRoot = context.extensionPath;
    this.globalStorage = context.globalStorageUri.fsPath;
  }

  get pythonDir(): string {
    return path.join(this.extRoot, 'python');
  }

  get venvPython(): string {
    return process.platform === 'win32'
      ? path.join(this.pythonDir, '.venv', 'Scripts', 'python.exe')
      : path.join(this.pythonDir, '.venv', 'bin', 'python');
  }

  get serverPy(): string {
    return path.join(this.pythonDir, 'server.py');
  }

  isRunning(): boolean {
    if (!this.handle) {
      return false;
    }
    return this.handle.process === null || this.handle.process.exitCode === null;
  }

  getHandle(): BackendHandle | null {
    return this.handle;
  }

  async ensureVenv(): Promise<string> {
    if (await this.venvReady()) {
      output.log(t('backend.logEnvCached'));
      return this.venvPython;
    }

    // 真正需要准备环境 (首次运行 / venv 损坏): 展开输出面板, 让用户看到真实进度
    output.step(t('backend.stepInitEnv'));
    output.reveal();

    // 环境是跨窗口共享资源 (extension/python/.venv + globalStorage/python-runtime),
    // 多窗口同时初始化会互相踩踏 (共用 tarPath、删除对方产物), 故用文件锁串行化。
    const release = await this.acquireEnvLock();
    try {
      // 拿到锁后重新检查: 持锁窗口可能刚把环境建好, 直接复用即可
      if (await this.venvReady()) {
        output.log(t('backend.logEnvCached'));
        return this.venvPython;
      }
      return await this.prepareVenv();
    } finally {
      await release();
    }
  }

  /** venv 是否已就绪 (存在 + 依赖齐全 + 版本达标) */
  private async venvReady(): Promise<boolean> {
    return (
      fs.existsSync(this.venvPython) &&
      (await this.venvDepsReady()) &&
      (await this.venvVersionOk())
    );
  }

  /** 真正创建 venv 并安装依赖 (调用方须已持有环境锁) */
  private async prepareVenv(): Promise<string> {
    // 主机优先: 有 >=3.9 的 Python 直接用; 若建 venv 失败 (如缺 python3-venv) 才回退内置
    let hostErr: unknown = null;
    const host = await this.resolvePython();
    if (host) {
      output.log(t('backend.logHostPython', { py: host.cmd }));
      try {
        await this.buildVenv(host);
        return this.venvPython;
      } catch (e) {
        hostErr = e;
        output.log(t('backend.logHostFailed', { py: host.cmd, err: errMsg(e) }));
        console.warn(`[qpiSdk] 主机 Python (${host.cmd}) 初始化 venv 失败, 回退内置运行时:`, e);
        await this.removeBrokenVenv();
      }
    } else {
      output.log(t('backend.logNoHostPython'));
    }

    const embedded = await this.ensureEmbeddedPython();
    if (!embedded) {
      // 主机建 venv 失败且内置运行时不可用: 优先报主机具体错误, 便于定位
      throw hostErr ?? new Error(t('backend.noPython'));
    }
    await this.buildVenv(embedded);
    return this.venvPython;
  }

  /** 环境准备锁文件 (globalStorage 下, 所有窗口同一用户共享) */
  private get lockPath(): string {
    return path.join(this.globalStorage, 'env-init.lock');
  }

  /** 获取环境准备锁: 抢不到时等待持锁窗口完成; 持锁者已死/心跳停止则夺锁重试。
   *  返回释放函数, 调用方必须在 finally 中 await 调用它。 */
  private async acquireEnvLock(): Promise<() => Promise<void>> {
    await fs.promises.mkdir(this.globalStorage, { recursive: true });
    const nonce = `${process.pid}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
    return vscode.window.withProgress(
      { location: vscode.ProgressLocation.Notification, title: t('backend.waitingOtherWindow'), cancellable: false },
      async () => {
        const startWait = Date.now();
        let waiting = false;
        let lastLog = 0;
        for (;;) {
          try {
            const fd = await fs.promises.open(this.lockPath, 'wx');
            await fd.writeFile(JSON.stringify({ pid: process.pid, startedAt: Date.now(), nonce }));
            await fd.close();
            this.startHeartbeat();
            return async () => this.releaseEnvLock(nonce);
          } catch (e) {
            if ((e as NodeJS.ErrnoException).code !== 'EEXIST') { throw e; }
          }
          // 锁已存在: 持锁者仍存活则等待; 已死/心跳停止/等待过久则夺锁
          const forced = Date.now() - startWait > LOCK_MAX_WAIT_MS;
          if (forced || (await this.isLockStale())) {
            output.log(t('backend.logStaleLock'));
            await fs.promises.rm(this.lockPath, { force: true }).catch(() => {});
            continue;
          }
          if (!waiting) {
            waiting = true;
            lastLog = Date.now();
            output.log(t('backend.logWaitLock'));
          } else if (Date.now() - lastLog > 15000) {
            lastLog = Date.now();
            output.log(t('backend.logWaitStill'));
          }
          await delay(500);
        }
      },
    );
  }

  /** 锁是否陈旧: 内容不可解析 / 持锁进程已死 / 心跳 (mtime) 超时, 任一成立即视为陈旧 */
  private async isLockStale(): Promise<boolean> {
    try {
      const [raw, st] = await Promise.all([
        fs.promises.readFile(this.lockPath, 'utf8'),
        fs.promises.stat(this.lockPath),
      ]);
      let pid = 0;
      try {
        pid = (JSON.parse(raw) as { pid?: number }).pid ?? 0;
      } catch {
        return true; // 内容损坏 → 陈旧
      }
      if (pid > 0 && !this.pidAlive(pid)) { return true; }
      return Date.now() - st.mtimeMs > LOCK_STALE_MS;
    } catch {
      return false; // 读不到 (可能刚被释放) → 不算陈旧, 下一轮重试
    }
  }

  /** 进程是否存活 (ESRCH 视为已退出, EPERM 说明存在但无权信号 ⇒ 存活) */
  private pidAlive(pid: number): boolean {
    try {
      process.kill(pid, 0);
      return true;
    } catch (e) {
      return (e as NodeJS.ErrnoException).code === 'EPERM';
    }
  }

  /** 持有锁期间定时刷新锁文件 mtime, 避免长下载/解压被误判为陈旧 */
  private startHeartbeat(): void {
    this.stopHeartbeat();
    this.heartbeat = setInterval(() => {
      const now = new Date();
      fs.promises.utimes(this.lockPath, now, now).catch(() => {});
    }, LOCK_HEARTBEAT_MS);
    this.heartbeat.unref?.();
  }

  private stopHeartbeat(): void {
    if (this.heartbeat) {
      clearInterval(this.heartbeat);
      this.heartbeat = null;
    }
  }

  /** 释放锁: 只删除属于自己的那把 (nonce 匹配), 避免误删被他人夺走的锁 */
  private async releaseEnvLock(nonce: string): Promise<void> {
    this.stopHeartbeat();
    try {
      const raw = await fs.promises.readFile(this.lockPath, 'utf8');
      if ((JSON.parse(raw) as { nonce?: string }).nonce === nonce) {
        await fs.promises.rm(this.lockPath, { force: true });
      }
    } catch {
      /* 已被清理或读取失败, 忽略 */
    }
  }

  /** 用指定解释器创建 venv 并安装依赖 (失败抛错, 由调用方回退到下一候选)。
   *  进度同时体现在两处: ① 右下角通知进度条 ② "QPi SDK" 输出面板的实时日志。 */
  private async buildVenv(py: { cmd: string; launcherArgs: string[] }): Promise<void> {
    await vscode.window.withProgress(
      {
        location: vscode.ProgressLocation.Notification,
        title: t('backend.initEnv'),
        cancellable: false,
      },
      async (progress) => {
        progress.report({ message: t('backend.stepCheckPython'), increment: 5 });
        // venv 存在但不可用 (版本过旧 <3.9 或连 pip 都没有): 删除重建
        const broken =
          fs.existsSync(this.venvPython) &&
          (!(await this.venvHasPip()) || !(await this.venvVersionOk()));
        if (broken) {
          output.log(t('backend.logBrokenVenv'));
          progress.report({ message: t('backend.stepRemoveBroken') });
          await this.removeBrokenVenv();
        }
        if (!fs.existsSync(this.venvPython)) {
          output.log(t('backend.logCreateVenv'));
          progress.report({ message: t('backend.stepCreateVenv') });
          await this.exec(py.cmd, ['-m', 'venv', path.join(this.pythonDir, '.venv')], py.launcherArgs);
          output.log(t('backend.logVenvCreated'));
        }
        progress.report({ increment: 15 });
        // pip 依赖安装: 按网络环境选镜像 (用户配置优先), 失败回退下一个镜像
        const mirrors = await pipMirrorOrder();
        const req = path.join(this.pythonDir, 'requirements.txt');
        let lastErr: unknown = null;
        for (const m of mirrors) {
          try {
            output.log(t('backend.logPipMirror', { mirror: m }));
            progress.report({ message: t('backend.stepInstallDeps') });
            await this.pipInstall(m, req, progress);
            lastErr = null;
            break;
          } catch (e) {
            lastErr = e;
            output.log(t('backend.logPipFail', { mirror: m, err: errMsg(e) }));
            console.warn(`[qpiSdk] pip 依赖安装失败 (${m}):`, e);
          }
        }
        if (lastErr) { throw lastErr; }
        output.log(t('backend.logEnvReady'));
      },
    );
  }

  /** 安装 requirements.txt: pip 输出逐行写入输出面板, 并按已下载包个数推进进度条。
   *  依赖总数未知 (含传递依赖), 故用固定小增量并封顶 70%, 避免进度条提前跑满后长时间不动。 */
  private pipInstall(
    mirror: string,
    req: string,
    progress: vscode.Progress<{ message?: string; increment?: number }>,
  ): Promise<void> {
    let acc = 0;
    const bump = (pkg: string): void => {
      const inc = Math.min(2, 70 - acc);
      if (inc <= 0) { return; }
      acc += inc;
      progress.report({ message: t('backend.installing', { pkg }), increment: inc });
    };
    return this.runLogged(
      this.venvPython,
      [
        '-m', 'pip', 'install',
        '--progress-bar', 'off',
        '--disable-pip-version-check',
        '--no-input',
        '-i', mirror,
        '-r', req,
      ],
      (line) => {
        const m = line.match(/^(?:Collecting|Downloading|Installing|Building)\s+(\S+)/);
        if (m) { bump(m[1]); }
      },
      // pip 走代理 (no_proxy 已含国内镜像源, 命中镜像时自动直连)
      proxyEnv(),
    );
  }

  /** 删除已存在但不可用的 venv (先改名挪走再后台清理, 规避 Windows 递归删除报 ENOTEMPTY) */
  private async removeBrokenVenv(): Promise<void> {
    const oldDir = path.join(this.pythonDir, '.venv');
    if (!fs.existsSync(oldDir)) { return; }
    const backupDir = path.join(this.pythonDir, `.venv.broken-${Date.now()}`);
    try {
      await fs.promises.rename(oldDir, backupDir);
      fs.promises.rm(backupDir, { recursive: true, force: true }).catch(() => {});
    } catch {
      await fs.promises.rm(oldDir, { recursive: true, force: true });
    }
  }

  /** 校验 venv 里依赖是否装齐 (静默 import 检查) */
  private async venvDepsReady(): Promise<boolean> {
    return new Promise((resolve) => {
      // 注意: pyserial 的 pip 包名是 pyserial, 但导入名是 serial
      const p = spawn(this.venvPython, ['-c', 'import fastapi, uvicorn, paramiko, pydantic, serial'], {
        stdio: ['ignore', 'ignore', 'ignore'],
      });
      p.on('exit', (code) => resolve(code === 0));
      p.on('error', () => resolve(false));
    });
  }

  /** 校验 venv 是否可用 (含 pip, 排除残缺 venv) */
  private async venvHasPip(): Promise<boolean> {
    return new Promise((resolve) => {
      const p = spawn(this.venvPython, ['-m', 'pip', '--version'], {
        stdio: ['ignore', 'ignore', 'ignore'],
      });
      p.on('exit', (code) => resolve(code === 0));
      p.on('error', () => resolve(false));
    });
  }

  /** 校验已有 venv 的 python 版本是否 >=3.9 (asyncio.to_thread 等依赖) */
  private async venvVersionOk(): Promise<boolean> {
    return (await this.pythonVersion(this.venvPython, [])) >= 309;
  }

  /** 探测可用 Python: 优先用户配置; 其次按平台选最新 3.x, 要求 >=3.9. 找不到返回 null */
  private async resolvePython(): Promise<{ cmd: string; launcherArgs: string[] } | null> {
    const configured = vscode.workspace.getConfiguration('qpiSdk').get<string>('backend.python') || '';
    if (configured) {
      // 用户显式指定的也校验版本, 避免用旧版建 venv 后陷入死循环
      return (await this.pythonVersion(configured, [])) >= 309 ? { cmd: configured, launcherArgs: [] } : null;
    }
    const candidates: Array<{ cmd: string; launcherArgs: string[] }> =
      process.platform === 'win32'
        ? [
            { cmd: 'py', launcherArgs: ['-3'] }, // py 启动器: 自动选最新 3.x
            { cmd: 'python', launcherArgs: [] },
            { cmd: 'python3', launcherArgs: [] },
          ]
        : [
            { cmd: 'python3', launcherArgs: [] },
            { cmd: 'python', launcherArgs: [] },
          ];
    for (const c of candidates) {
      if ((await this.pythonVersion(c.cmd, c.launcherArgs)) >= 309) {
        return c;
      }
    }
    // 没有任何 >=3.9 的 Python: 返回 null, 由 ensureVenv 给出明确提示
    return null;
  }

  /** 内置运行时根目录 (用户 globalStorage, 不占插件安装目录) */
  private get runtimeRoot(): string {
    return path.join(this.globalStorage, 'python-runtime');
  }

  /** 内置运行时中的 python 可执行文件, 不存在返回 null */
  private embeddedPythonPath(): string | null {
    const exe =
      process.platform === 'win32'
        ? path.join(this.runtimeRoot, 'python', 'python.exe')
        : path.join(this.runtimeRoot, 'python', 'bin', 'python3');
    return fs.existsSync(exe) ? exe : null;
  }

  /** 自动下载内置 Python 运行时 (python-build-standalone install_only, 含 pip/venv)。
   *  成功返回 { cmd, launcherArgs }; 平台不支持 / 下载或解压失败返回 null (原因记日志)。
   *  解压到本窗口独占的 staging 目录, 校验通过后再原子替换正式目录, 避免多窗口互相踩踏。 */
  private async ensureEmbeddedPython(): Promise<{ cmd: string; launcherArgs: string[] } | null> {
    const existing = this.embeddedPythonPath();
    if (existing && (await this.pythonVersion(existing, [])) >= 309) {
      return { cmd: existing, launcherArgs: [] };
    }
    const target = pbsTarget();
    if (!target) {
      console.error(`[qpiSdk] 当前平台 ${process.platform}/${process.arch} 不支持自动下载 Python 运行时`);
      output.log(t('backend.logPythonUnsupported', { platform: `${process.platform}/${process.arch}` }));
      return null;
    }
    // 下载源: 按网络环境自动选择 (用户配置优先), 失败时回退下一个镜像
    const prefixes = await pythonMirrorOrder();
    const file = pbsFile(target);
    const urls = prefixes.map((p) => `${p}/${PY_BUILD_TAG}/${file}`);
    // 本窗口独占的 staging 目录: 不与其它窗口共用 tarPath / 正式目录
    const staging = `${this.runtimeRoot}.staging-${process.pid}-${Date.now().toString(36)}`;
    const tarPath = path.join(staging, file);
    try {
      await this.cleanupStaleRuntimeDirs();
      await fs.promises.mkdir(staging, { recursive: true });
      output.step(t('backend.fetchingPython'));
      output.reveal();
      await vscode.window.withProgress(
        {
          location: vscode.ProgressLocation.Notification,
          title: t('backend.fetchingPython'),
          cancellable: false,
        },
        async (progress) => {
          let lastErr: unknown = null;
          for (let i = 0; i < urls.length; i++) {
            try {
              output.log(t('backend.logPythonUrl', { url: urls[i] }));
              const { got, total } = await downloadFile(urls[i], tarPath, {
                progress,
                logProgress: i === 0,
                onPct: (pct, g, tt) =>
                  output.log(t('backend.logPythonPct', {
                    pct: String(pct), got: fmtBytes(g), total: fmtBytes(tt),
                  })),
              });
              // 下载后先校验完整性: 内容被网络/代理污染或截断时换下一个镜像重试,
              // 否则会把坏文件交给 tar, 只能得到无信息的 code=2
              await this.verifyRuntimeTarball(tarPath, got, total);
              lastErr = null;
              break;
            } catch (e) {
              lastErr = e;
              await fs.promises.rm(tarPath, { force: true }).catch(() => {});
              output.log(t('backend.logPythonUrlFail', { url: urls[i], err: errMsg(e) }));
              console.warn(`[qpiSdk] Python 运行时下载失败 (${urls[i]}):`, e);
            }
          }
          if (lastErr) { throw lastErr; }
          progress.report({ message: t('backend.extractingPython'), increment: 0 });
          output.log(t('backend.extractingPython'));
          await this.execTar(tarPath, staging);
        },
      );
      // 先校验 staging 里的运行时可用, 再原子替换, 避免半成品污染正式目录
      const staged = process.platform === 'win32'
        ? path.join(staging, 'python', 'python.exe')
        : path.join(staging, 'python', 'bin', 'python3');
      if (!(fs.existsSync(staged) && (await this.pythonVersion(staged, [])) >= 309)) {
        console.error('[qpiSdk] 自动下载的 Python 运行时不可用 (版本校验失败)');
        output.log(t('backend.logPythonBad'));
        return null;
      }
      await this.promoteRuntime(staging);
      const py = this.embeddedPythonPath();
      if (py && (await this.pythonVersion(py, [])) >= 309) {
        output.log(t('backend.logPythonReady'));
        return { cmd: py, launcherArgs: [] };
      }
      console.error('[qpiSdk] 提升后的 Python 运行时校验失败');
      return null;
    } catch (e) {
      console.error('[qpiSdk] 自动下载 Python 运行时失败:', e);
      output.log(t('backend.logPythonFail', { err: errMsg(e) }));
      return null;
    } finally {
      // 只清理本窗口的 staging, 绝不删除正式 runtimeRoot (可能是别的窗口的成果)
      await fs.promises.rm(staging, { recursive: true, force: true }).catch(() => {});
    }
  }

  /** 把 staging 目录原子替换为正式运行时目录 (调用方须已持有环境锁) */
  private async promoteRuntime(staging: string): Promise<void> {
    const target = this.runtimeRoot;
    if (fs.existsSync(target)) {
      // 已存在但不可用 (上次残留): 先挪走再放入新的, 后台清理 (规避 Windows 递归删除报错)
      const backup = `${target}.old-${Date.now().toString(36)}`;
      try {
        await fs.promises.rename(target, backup);
        fs.promises.rm(backup, { recursive: true, force: true }).catch(() => {});
      } catch {
        await fs.promises.rm(target, { recursive: true, force: true });
      }
    }
    await fs.promises.rename(staging, target);
  }

  /** 清理上次中断残留的 staging / 旧目录 (调用方须已持有环境锁: 此时不会有其它窗口在写) */
  private async cleanupStaleRuntimeDirs(): Promise<void> {
    const base = path.basename(this.runtimeRoot);
    let entries: string[];
    try {
      entries = await fs.promises.readdir(this.globalStorage);
    } catch {
      return;
    }
    await Promise.all(
      entries
        .filter((n) => n.startsWith(`${base}.staging-`) || n.startsWith(`${base}.old-`))
        .map((n) => fs.promises.rm(path.join(this.globalStorage, n), { recursive: true, force: true }).catch(() => {})),
    );
  }

  /** 校验下载的运行时压缩包: 大小与 Content-Length 一致, 且开头为 gzip 魔数 (1f 8b)。
   *  不通过则抛错, 由上层删除文件并换下一个镜像重试。 */
  private async verifyRuntimeTarball(file: string, got: number, total: number): Promise<void> {
    if (total > 0 && got !== total) {
      throw new Error(t('backend.downloadIncomplete', { got: fmtBytes(got), total: fmtBytes(total) }));
    }
    const fh = await fs.promises.open(file, 'r');
    try {
      const buf = Buffer.alloc(2);
      await fh.read(buf, 0, 2, 0);
      if (buf[0] !== 0x1f || buf[1] !== 0x8b) {
        throw new Error(t('backend.downloadNotGzip'));
      }
    } finally {
      await fh.close();
    }
  }

  /** 用系统 tar 解压 (Win10 1803+ / Linux / macOS 均自带, 支持 tar.gz)。
   *  保留 stderr 尾部并写入输出面板: 对端环境问题 (非 gzip / 磁盘满 / 权限) 只能从这里看出。 */
  private async execTar(tarFile: string, destDir: string): Promise<void> {
    return new Promise((resolve, reject) => {
      const p = spawn('tar', ['-xzf', tarFile, '-C', destDir], { stdio: ['ignore', 'ignore', 'pipe'] });
      const errLines: string[] = [];
      p.stderr?.on('data', (d: Buffer) => {
        for (const raw of d.toString().split(/\r?\n/)) {
          const line = raw.replace(/\s+$/, '');
          if (line) { errLines.push(line); }
        }
      });
      p.on('exit', (code) => {
        if (code === 0) { resolve(); return; }
        const tail = errLines.slice(-8).join('\n');
        if (tail) { output.log(tail); }
        const msg = t('backend.tarExtractFailed', { code: String(code) });
        reject(new Error(tail ? `${msg}\n${tail}` : msg));
      });
      p.on('error', (e) => reject(new Error(t('backend.tarUnavailable', { err: e.message }))));
    });
  }

  /** 运行 <cmd> [launcherArgs] --version 并返回 major*100+minor, 失败/非 Python 返回 0 */
  private pythonVersion(cmd: string, launcherArgs: string[]): Promise<number> {
    return new Promise((resolve) => {
      const p = spawn(cmd, [...launcherArgs, '--version'], { stdio: ['ignore', 'pipe', 'pipe'] });
      let out = '';
      p.stdout?.on('data', (d) => (out += d.toString()));
      p.stderr?.on('data', (d) => (out += d.toString())); // python 将版本信息打到 stderr
      p.on('error', () => resolve(0));
      p.on('exit', () => {
        const m = out.match(/Python (\d+)\.(\d+)/);
        resolve(m ? parseInt(m[1], 10) * 100 + parseInt(m[2], 10) : 0);
      });
    });
  }

  /** 启动后端, 等待 READY 握手 (并发调用共享同一次启动, 避免重复初始化) */
  async start(): Promise<BackendHandle> {
    if (this.isRunning()) {
      return this.handle!;
    }
    if (!this.starting) {
      const p = this.doStart();
      this.starting = p;
      p.then(
        () => {
          if (this.starting === p) {
            this.starting = null;
          }
        },
        () => {
          if (this.starting === p) {
            this.starting = null;
          }
        },
      );
    }
    return this.starting;
  }

  private async doStart(): Promise<BackendHandle> {
    // 远程直连模式: 不本地拉起后端, 直接使用配置的地址 (后端跑在 SDK 机器上)
    const mode = vscode.workspace.getConfiguration('qpiSdk').get<string>('backend.mode', 'local');
    if (mode === 'remote') {
      const url = vscode.workspace.getConfiguration('qpiSdk').get<string>('backend.remoteUrl', 'http://127.0.0.1:8765');
      const key = vscode.workspace.getConfiguration('qpiSdk').get<string>('backend.remoteKey', '');
      const m = url.match(/^https?:\/\/([\w.-]+):(\d+)/);
      if (!m) {
        throw new Error(t('backend.invalidRemoteUrl', { url }));
      }
      this.handle = { host: m[1], port: parseInt(m[2], 10), key, process: null };
      output.log(t('backend.logRemoteMode', { host: this.handle.host, port: String(this.handle.port) }));
      return this.handle;
    }
    await this.ensureVenv();
    output.log(t('backend.logStarting'));
    const proc = spawn(this.venvPython, [this.serverPy, '--port', '0'], {
      cwd: this.pythonDir,
      stdio: ['ignore', 'pipe', 'pipe'],
      env: {
        ...process.env,
        // 让后端能找到插件内置的 adb (<ext>/tools/windows/adb); 烧录工具随 SDK 分发
        QPI_SDK_EXT_TOOLS: path.join(this.extRoot, 'tools'),
        // 后端 UI 文案语言 (默认中文); 语言切换后重启后端即可全量生效
        QPILANG: currentLanguage(),
        // 统一下载策略: 标准代理环境变量 (供 git/curl/wget/apt 等子进程继承) +
        // QPI_DL_* (供 Python 侧解析镜像顺序与 SDK 下载)
        ...proxyEnv(),
        ...backendDownloadEnv(),
      },
    });
    this.pendingProc = proc;
    try {
      const handle = await new Promise<BackendHandle>((resolve, reject) => {
        const timeout = setTimeout(() => {
          reject(new Error(t('backend.startTimeout')));
          proc.kill();
        }, 15000);
        let buf = '';
        const onData = (chunk: Buffer) => {
          buf += chunk.toString();
          const m = buf.match(/READY port=(\d+) key=(\S+)/);
          if (m) {
            clearTimeout(timeout);
            output.log(t('backend.logReady', { port: m[1] }));
            resolve({ host: '127.0.0.1', port: parseInt(m[1], 10), key: m[2], process: proc });
          }
        };
        proc.stdout!.on('data', onData);
        proc.stderr!.on('data', (d: Buffer) => {
          const s = d.toString().trim();
          if (s) {
            console.error('[qpiSdk-backend]', s);
          }
        });
        proc.on('error', (e) => {
          clearTimeout(timeout);
          reject(e);
        });
        proc.on('exit', (code) => {
          if (!this.handle) {
            clearTimeout(timeout);
            reject(new Error(t('backend.procExited', { code: String(code) })));
          }
        });
      });

      this.handle = handle;
      return handle;
    } finally {
      if (this.pendingProc === proc) {
        this.pendingProc = null;
      }
    }
  }

  /** 停止后端 (remote 模式不杀远端进程) */
  stop(): void {
    this.starting = null; // 使在途启动不再被后续 start 复用
    if (this.pendingProc) {
      try {
        this.pendingProc.kill('SIGKILL');
      } catch {
        /* 已退出 */
      }
      this.pendingProc = null;
    }
    if (this.handle) {
      if (this.handle.process) {
        try {
          this.handle.process.kill('SIGKILL');
        } catch {
          /* 已退出 */
        }
      }
      this.handle = null;
    }
  }

  private exec(cmd: string, args: string[], launcherArgs: string[] = []): Promise<void> {
    return this.runLogged(cmd, [...launcherArgs, ...args]);
  }

  /** 执行子进程并把 stdout/stderr 逐行写入 "QPi SDK" 输出面板。
   *  onLine 可基于输出推进进度条; 失败时把 stderr 尾部附加到错误信息里便于定位。 */
  private runLogged(
    cmd: string,
    args: string[],
    onLine?: (line: string) => void,
    env?: NodeJS.ProcessEnv,
  ): Promise<void> {
    return new Promise((resolve, reject) => {
      const p = spawn(cmd, args, {
        stdio: ['ignore', 'pipe', 'pipe'],
        env: env && Object.keys(env).length ? { ...process.env, ...env } : process.env,
      });
      const errLines: string[] = [];
      const handle = (chunk: Buffer, isErr: boolean): void => {
        for (const raw of chunk.toString().split(/\r?\n/)) {
          const line = raw.replace(/\s+$/, '');
          if (!line) { continue; }
          if (isErr) { errLines.push(line); }
          output.log(line);
          onLine?.(line);
        }
      };
      p.stdout?.on('data', (d: Buffer) => handle(d, false));
      p.stderr?.on('data', (d: Buffer) => handle(d, true));
      p.on('error', reject);
      p.on('exit', (code) => {
        if (code === 0) { resolve(); return; }
        const tail = errLines.slice(-8).join('\n');
        const msg = t('backend.cmdExit', { cmd, code: String(code) });
        reject(new Error(tail ? `${msg}\n${tail}` : msg));
      });
    });
  }
}
