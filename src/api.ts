/* 后端 HTTP API 客户端: 请求 + SSE 流式订阅 */
import { BackendHandle } from './backend';
import { t, currentLanguage } from './i18n';

export interface TaskInfo {
  task_id: string;
}

export interface DeviceTreeNode {
  name: string;
  enabled: boolean;
  properties: Record<string, string>;
  path?: string;
  source?: string;
  sourceLabel?: string;
}

export interface DeviceTreeSource {
  id: string;
  label: string;
  kind: string;
  soc?: string;
  nodes: DeviceTreeNode[];
}

export interface DeviceTreeData {
  path: string;
  error?: string;
  sources: DeviceTreeSource[];
}

export interface DeviceInfoData {
  ok: boolean;
  error?: string;
  device?: { serial: string; state: string };
  system?: {
    model?: string;
    compatible?: string[];
    serial?: string;
    os?: string;
    kernel?: string;
    machine?: string;
    fw?: Record<string, string>;
    rootDev?: string;
  };
  hardware?: {
    cpuModel?: string;
    cpuCores?: number;
    load?: number[];
    memTotalMB?: number;
    memUsedMB?: number;
    memAvailableMB?: number;
  };
  storage?: {
    emmcMB?: number;
    partitions?: Array<{ name: string; sizeMB: number }>;
    mounts?: Array<{ fs: string; size: string; used: string; avail: string; usePct: number; mount: string }>;
  };
  network?: {
    hostname?: string;
    interfaces?: Array<{ name: string; ipv4?: string[]; mac?: string }>;
  };
  thermal?: Array<{ name: string; tempC?: number; noData?: boolean }>;
  // vbat/ibat-lvl*/bcl-lvl* 也是 thermal zone, 但原始值不是温度 (电压/电流/限流档位)
  thermalPower?: Array<{ name: string; value: number; unit?: string; kind: string }>;
  runtime?: { uptimeSec?: number; procs?: number };
}

/** 性能监控单次快照: 字段缺失表示该平台未暴露对应节点 (前端显示「无数据」) */
export interface PerfSampleData {
  ok: boolean;
  error?: string;
  ts?: number;
  device?: { serial: string; state: string };
  fps?: number;
  cpu?: {
    cores?: number;
    usagePct?: number;
    load?: number[];
    clusters?: Array<{ id: string; curMHz?: number; maxMHz?: number }>;
    avgMHz?: number;
    maxMHz?: number;
  };
  gpu?: { name?: string; curMHz?: number; maxMHz?: number; busyPct?: number };
  thermal?: { cpuTempC?: number; gpuTempC?: number; ddrTempC?: number; socTempC?: number; maxTempC?: number };
  platform?: { board?: string; soc?: string; family?: string };
}

export interface ConnConfig {
  mode: 'local' | 'ssh';
  host?: string;
  port?: number;
  user?: string;
  password?: string;
  key_path?: string;
  sdk_root: string;
  project_dir?: string;
  /**
   * 设备连接位置 (仅 ssh 模式有意义): local = 板子 USB 接本机,
   * remote = 板子 USB 接远端编译机。所有设备操作 (adb/AT/烧录/部署) 统一跟随。
   */
  device_location?: 'local' | 'remote';
}

/** SDK 平台标识 (由后端读取 tools/VERSION.txt 得出)。 */
export interface SdkPlatform {
  /** "qualcomm" (H1) 或 "rockchip" (M2) */
  family: 'qualcomm' | 'rockchip';
  /** 构建体系: "yocto" (Yocto/BitBake, 如 Pi-H1-Linux-SDK) 或 "simple" (原生 build.sh) */
  sdk_type?: 'yocto' | 'simple';
  board: string;
  soc: string;
  flash_protocol: string;
  /** SDK 自带全量烧录脚本 (相对 SDK 根), Qualcomm 平台使用; Rockchip 为空 */
  flash_entry: string;
  /** 进入下载模式的完整命令 (Qualcomm 的 ENTER_EDL); Rockchip 为空 */
  enter_loader: string;
  update_img_supported: boolean;
  source: string;
}

/** 单条 git 提交 (SDK 版本信息面板用) */
export interface GitCommit {
  hash: string;
  full: string;
  author: string;
  date: string;
  subject: string;
}

/** SDK 目录的 git 状态 (由后端 SDK.git_info 返回) */
export interface SdkGitInfo {
  ok: boolean;
  is_repo: boolean;
  error: string;
  sdk_root: string;
  branch: string;
  commit: string;
  commit_full: string;
  subject: string;
  author: string;
  date: string;
  remote: string;
  upstream: string;
  has_upstream: boolean;
  /** 落后远端的提交数 (= 需要更新多少个版本) */
  behind: number;
  ahead: number;
  up_to_date: boolean;
  latest_remote: GitCommit | null;
  log: GitCommit[];
  /** git fetch 是否成功; false 时 behind/ahead 基于上次已知的远端状态 */
  fetch_ok: boolean;
  shallow: boolean;
}

/** 从 SSE 流中解析出的单行输出 */
export type StreamEvent =
  | { type: 'out' | 'err'; line: string }
  /** 原样透传的终端输出 (含 \r 与 ANSI 转义, 未拆行): 用于 pty 下的交互式输出,
   *  必须直接写入终端, 由终端模拟器处理光标移动/原地刷新。 */
  | { type: 'raw'; data: string }
  | { type: 'done'; exit_code: number | null; status: string }
  | { type: 'meta'; task_id: string; cmd: string }
  | { type: 'progress'; total: number; done: number; pct: number; label?: string };

export class ApiClient {
  constructor(private handle: () => BackendHandle | null) {}

  private base(): string {
    const h = this.handle();
    if (!h) {
      throw new Error(t('backend.notRunning'));
    }
    return `http://${h.host}:${h.port}/api`;
  }

  private headers(extra: Record<string, string> = {}): Record<string, string> {
    const h = this.handle();
    // X-QPi-Lang: 让后端按当前 UI 语言返回用户可见文案 (后端默认中文)
    return { 'X-API-Key': h!.key, 'Content-Type': 'application/json', 'X-QPi-Lang': currentLanguage(), ...extra };
  }

  private async req<T>(method: string, url: string, body?: unknown): Promise<T> {
    const resp = await fetch(this.base() + url, {
      method,
      headers: this.headers(),
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (!resp.ok) {
      const text = await resp.text();
      let detail = text.slice(0, 300);
      try {
        detail = JSON.parse(text).detail || detail;
      } catch {
        /* ignore */
      }
      throw new Error(detail);
    }
    return resp.json() as Promise<T>;
  }

  // ---- 状态 ----
  status = () => this.req<{ ok: boolean; connected: boolean; conn: ConnConfig | null; display: string; platform: SdkPlatform | null }>('GET', '/status');
  connect = (conn: ConnConfig) => this.req<{ ok: boolean; probe?: unknown }>('POST', '/connect', { conn });
  disconnect = () => this.req<{ ok: boolean }>('POST', '/disconnect');
  probe = () => this.req<Record<string, unknown>>('GET', '/probe');

  // ---- 探测 ----
  devices = () => this.req<{ devices: Array<{ serial: string; state: string }> }>('GET', '/devices');
  skills = () => this.req<{ skills: Array<{ name: string; path: string; size: number }> }>('GET', '/skills');
  skill = (name: string) => this.req<{ name: string; content: string }>('GET', `/skill?name=${encodeURIComponent(name)}`);
  templates = () => this.req<{ templates: Array<Record<string, unknown>> }>('GET', '/templates');
  projects = () => this.req<{ projects: Array<{ name: string; dir: string; hasBinary: boolean }> }>('GET', '/projects');
  examples = () => this.req<{ examples: string[] }>('GET', '/examples');
  flashTools = () => this.req<{ tools: Array<{ name: string; hint_key?: string; hint_vars?: Record<string, string>; arch_warn?: [string, string]; found: boolean; path: string; bundled?: boolean; kind?: string }>; platform: SdkPlatform | null }>('GET', '/flash-tools');
  ports = () => this.req<{ ports: Array<{ device: string; desc: string; kind?: string; adb_serial?: string }> }>('GET', '/ports');
  deviceInfo = () => this.req<DeviceInfoData>('GET', '/device/info');
  deviceTree = () => this.req<DeviceTreeData>('GET', '/device-tree');
  setDeviceTreeStatus = (node: string, enabled: boolean, source = '') =>
    this.req<{ ok: boolean; node: string; enabled: boolean }>('POST', '/device-tree/status', { node, enabled, source });
  setDeviceTreeProperties = (node: string, source: string, properties: string) =>
    this.req<{ ok: boolean; node: string }>('POST', '/device-tree/properties', { node, source, properties });
  deviceTreeSource = (node: string, label = '') =>
    this.req<{ source: string; node: string }>('POST', '/device-tree/source', { node, label });

  // ---- 任务 ----
  exec = (cmd: string, cwd = '') => this.req<TaskInfo>('POST', '/exec', { cmd, cwd });
  killTask = (id: string) => this.req<{ ok: boolean }>('POST', `/tasks/${id}/kill`);
  tasks = () => this.req<{ tasks: Array<{ id: string; cmd: string; status: string; exit_code: number | null }> }>('GET', '/tasks');

  /** 订阅任务 SSE 输出 (fetch + 手动解析, 支持自定义 header) */
  async stream(taskId: string, onEvent: (e: StreamEvent) => void, onError: (err: string) => void): Promise<void> {
    const h = this.handle();
    if (!h) {
      throw new Error(t('backend.notRunning'));
    }
    const resp = await fetch(`http://${h.host}:${h.port}/api/tasks/${taskId}/stream`, {
      headers: { 'X-API-Key': h.key, 'X-QPi-Lang': currentLanguage() },
    });
    if (!resp.ok || !resp.body) {
      throw new Error(`stream ${resp.status}`);
    }
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    let eventType = '';
    let dataBuf = '';
    const flushEvent = () => {
      if (!eventType || !dataBuf) {
        return;
      }
      const data = dataBuf.trim();
      try {
        if (eventType === 'out' || eventType === 'err') {
          onEvent({ type: eventType, line: JSON.parse(data) });
        } else if (eventType === 'raw') {
          onEvent({ type: 'raw', data: JSON.parse(data) });
        } else if (eventType === 'done') {
          const d = JSON.parse(data);
          onEvent({ type: 'done', exit_code: d.exit_code, status: d.status });
        } else if (eventType === 'meta') {
          const d = JSON.parse(data);
          onEvent({ type: 'meta', task_id: d.task_id, cmd: d.cmd });
        } else if (eventType === 'progress') {
          const d = JSON.parse(data);
          onEvent({ type: 'progress', total: d.total, done: d.done, pct: d.pct, label: d.label });
        }
      } catch (e) {
        onError(t('api.sseParseError', { err: String(e) }));
      }
      eventType = '';
      dataBuf = '';
    };
    // eslint-disable-next-line no-constant-condition
    while (true) {
      const { done, value } = await reader.read();
      if (done) {
        break;
      }
      buf += decoder.decode(value, { stream: true });
      let idx: number;
      while ((idx = buf.indexOf('\n\n')) >= 0) {
        const block = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        for (const line of block.split('\n')) {
          if (line.startsWith('event: ')) {
            eventType = line.slice(7);
          } else if (line.startsWith('data: ')) {
            dataBuf += line.slice(6);
          }
        }
        flushEvent();
      }
    }
  }

  // ---- 编译 ----
  /** password 仅在 rootfs/all 等需要 root 的模式下非空 (先经 sudoCheck 探测) */
  build = (mode: string, arg = '', password = '', threads = 0,
           yocto: Record<string, unknown> | null = null) =>
    this.req<TaskInfo>('POST', '/build', { mode, arg, password, threads, yocto });

  /** 探测该编译模式在非交互执行时是否需要 sudo 密码 (免密/不需要 root 则不打扰用户)。
   *  docker_missing=True 表示 SDK 环境未安装 docker (编译模式): 不需要密码, 应提示先安装/配置。 */
  sudoCheck = (mode: string) =>
    this.req<{ needed: boolean; remote: boolean; docker?: boolean; reason: string; docker_missing?: boolean; host_os?: string }>(
      'GET', `/sudo/check?mode=${encodeURIComponent(mode)}`);

  // ---- 构建环境 (委托给 SDK 自带 build.sh setenv) ----
  depsCheck = () => this.req<{ available: boolean; apt: boolean; missing: string[]; binfmt?: { needed: string[]; missing: string[]; ok: boolean; can_enable: boolean } }>('GET', '/deps/check');
  envSetup = (password: string) => this.req<TaskInfo>('POST', '/env/setup', { password });
  envBinfmt = (password: string) => this.req<TaskInfo>('POST', '/env/binfmt', { password });
  envBinfmtCheck = () => this.req<{ needed: string[]; missing: string[]; ok: boolean; can_enable: boolean }>('GET', '/env/binfmt');

  // ---- Docker (容器内编译) ----
  /** 探测 SDK 所在机器的 Docker 环境状态 (docker_env 目录 / docker 安装 / daemon / 镜像) */
  dockerStatus = () =>
    this.req<{ env_dir: boolean; docker_installed: boolean; daemon_ok: boolean; image_present: boolean; image: string; host_os: string; error: string }>(
      'GET', '/docker/status');
  /** 安装/配置 Docker 并构建 quectel-pi-builder 镜像 (幂等; password 为空=免密 sudo) */
  dockerSetup = (password: string) =>
    this.req<TaskInfo>('POST', '/docker/setup', { password });

  // ---- 烧录 ----
  flash = (tool: string, image = 'build/result/update.img', password = '') =>
    this.req<TaskInfo>('POST', '/flash', { tool, image, password });
  flashPartition = (tool: string, partition: string, image = '') =>
    this.req<TaskInfo>('POST', '/flash/partition', { tool, partition, image });
  deploy = (source: string, target: string, kind = 'auto', run = false, runCmd = '') =>
    this.req<TaskInfo>('POST', '/deploy', {
      source, target, kind, run, run_cmd: runCmd,
    });

  // ---- 工程 ----
  projectCreate = (name: string, template: string, variables: Record<string, unknown>) =>
    this.req<{ ok: boolean; dir?: string; error?: string }>('POST', '/project/create', { name, template, variables });
  projectDelete = (name: string) => this.req<{ ok: boolean; error?: string }>('POST', `/project/delete?name=${encodeURIComponent(name)}`);

  // ---- 设备 ----
  /** 解析本机 adb 路径; refresh=true 时先丢弃后端缓存重新解析 (装好 adb 后复查) */
  adbWhich = (refresh = false) =>
    this.req<{ path: string }>('GET', `/adb/which?refresh=${refresh ? 1 : 0}`);
  /** 检测本机 adb 环境 (是否可用 / 可否自动安装 / 是否需 sudo 密码) */
  adbEnvCheck = () =>
    this.req<{ found: boolean; path: string; host: string; is_windows: boolean;
               installable: boolean; pkg: string; need_sudo: boolean; reason: string }>(
      'GET', '/adb/env/check');
  /** 本机 (原生 Linux) 自动安装 adb; password 为空 = 免密 sudo */
  adbEnvSetup = (password: string) =>
    this.req<TaskInfo>('POST', '/adb/env/setup', { password });
  adb = (args: string) => this.req<TaskInfo>('POST', '/adb', { args });
  dmesg = (filter = '', follow = false) => this.req<TaskInfo>('POST', `/dmesg?filter_str=${encodeURIComponent(filter)}&follow=${follow}`);
  journalctl = (filter = '', follow = false) => this.req<TaskInfo>('POST', `/journalctl?filter_str=${encodeURIComponent(filter)}&follow=${follow}`);
  reboot = (edl = false) => this.req<TaskInfo>('POST', `/reboot?edl=${edl}`);
  rebootLoader = () => this.req<TaskInfo>('POST', '/reboot-loader');

  // ---- AT ----
  at = (cmds: string[], port = '/dev/ttyACM0', adb_serial = '', baud = 115200,
        bytesize = 8, parity = 'N', stopbits = 1, flow = 'none') =>
    this.req<TaskInfo>('POST', '/at', { cmds, port, adb_serial, baud, bytesize, parity, stopbits, flow });

  // ---- 音频 ----
  audioCollect = () => this.req<TaskInfo>('POST', '/audio/collect', {});
  tinymix = (args: string) => this.req<TaskInfo>('POST', '/audio/tinymix', { args });
  aplay = (file = '/tmp/test.wav') => this.req<TaskInfo>('POST', '/audio/aplay', { file });
  playVideo = (file = '/tmp/test.mp4') => this.req<TaskInfo>('POST', '/video/play', { file });

  // ---- 工具 ----
  screenshot = (filename = '') => this.req<TaskInfo>('POST', '/screenshot', { filename });
  screenRecord = (duration = '10') => this.req<TaskInfo>('POST', '/screen/record', { duration });
  perfSample = () => this.req<PerfSampleData>('GET', '/perf/sample');

  // ---- 仓库引导 ----
  repoStatus = (repos: Record<string, string>) => this.req<{ components: Array<{ label: string; rel: string; exists: boolean; url: string }> }>('POST', '/repo/status', { repos });
  repoClone = (rel: string, url: string, force = false) => this.req<TaskInfo>('POST', '/repo/clone', { rel, url, force });
  repoCloneCore = (url: string, targetDir: string) => this.req<TaskInfo>('POST', '/repo/clone-core', { url, target_dir: targetDir });
  // ---- SDK 下载 (管理SDK下载 -> 本机终端执行, 关闭终端即终止) ----
  sdkDownload = (version: string, repo: string, branch: string, archive: string, targetDir: string) =>
    this.req<TaskInfo>('POST', '/sdk/download', { version, repo, branch, archive, target_dir: targetDir });

  // ---- Yocto SDK (buildconfig 配置构建环境) ----
  yoctoBuildconfig = (projectId: string, os: string, version: string, flags: string[]) =>
    this.req<TaskInfo>('POST', '/yocto/buildconfig', { project_id: projectId, os, version, flags });
  /** Yocto 构建机资源 + 推荐并行线程数 (配置面板预填线程输入框) */
  yoctoHost = () =>
    this.req<{ cpu_count: number; mem_total_mb: number; swap_total_mb: number; suggested_threads: number; low_memory: boolean }>('GET', '/yocto/host');
  /** Yocto 构建环境检测: apt 包 / 命令(repo) / pyhocon / locale / 磁盘空间 */
  yoctoEnvCheck = () =>
    this.req<{ ok: boolean; missing_apt: string[]; missing_cmd: string[]; missing_py: string[]; locale_missing: boolean; need_sudo: boolean; disk_free_gb: number }>('GET', '/yocto/env/check');
  /** 烧录 Yocto 编译产物到设备分区: part = 'boot' (dtb.bin) | 'kernel' (efi.bin) | 'both' (两者) */
  yoctoDeploy = (part: 'boot' | 'kernel' | 'both') =>
    this.req<TaskInfo>('POST', '/yocto/deploy', { part });
  /** 自动安装 Yocto 缺失依赖 (需 root, 密码经 stdin 传递) */
  yoctoEnvSetup = (password: string) =>
    this.req<TaskInfo>('POST', '/yocto/env/setup', { password });

  // ---- SDK 版本 (git 状态) ----
  /** fetch=true 会联网刷新远端引用 (较慢但准确); false 只用本地已知的远端状态 */
  sdkGit = (fetch = true, logLimit = 20) =>
    this.req<SdkGitInfo>('GET', `/sdk/git?fetch=${fetch ? 1 : 0}&log_limit=${logLimit}`);
}
