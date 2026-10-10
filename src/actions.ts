/* 命令动作实现: 连接向导、参数输入、任务下发到原生终端、SKILL 文档浏览。
   复杂页面 (工程模板 / 设备树 / 仓库引导) 回退到完整控制台 Webview, 功能不丢失。 */
import * as vscode from 'vscode';
import * as path from 'path';
import * as fs from 'fs';
import { ApiClient, ConnConfig, SdkGitInfo, SdkPlatform } from './api';
import { BackendManager } from './backend';
import { TaskTerminalManager } from './taskterminal';
import { ConnectPanel } from './connect';
import { YoctoConfigPanel } from './yoctoConfigPanel';
import { ATPanel } from './atpanel';
import { DeviceTreePanel } from './devicetreePanel';
import { DeviceInfoPanel } from './deviceInfoPanel';
import { PerfPanel } from './perfPanel';
import { SdkInfoPanel } from './sdkInfoPanel';
import { t } from './i18n';
import { getYoctoThreads } from './yoctoThreads';
import { yoctoBuildPayload } from './yoctoBuildParams';
import * as output from './output';

interface SdkSource {
  repo?: string;   // git 仓库地址 (如 https://github.com/xxx/xxx.git)
  branch?: string; // git 分支名
  archive?: string;// 归档下载 URL (tar.gz/zip), 与 repo 二选一
}
const SDK_SOURCES: Record<string, SdkSource> = {
  M1: { repo: '', branch: '', archive: '' },
  M2: { repo: 'https://github.com/Quectel-Pi/QPi-SDK.git', branch: 'M2-SDK', archive: '' },
  H1: { repo: 'https://github.com/Quectel-Pi/QPi-SDK.git', branch: 'H1-SDK', archive: '' },
  // H1 Yocto SDK: 与 Simple SDK 是不同的仓库/构建体系 (Yocto/BitBake), 直接切 master
  H1_YOCTO: { repo: 'https://github.com/Quectel-Pi/Pi-H1-Linux-SDK.git', branch: 'master', archive: '' },
};

async function input(prompt: string, value = '', password = false): Promise<string | undefined> {
  return vscode.window.showInputBox({ prompt, value, password });
}

async function pick(items: string[] | Thenable<string[]>, placeHolder: string): Promise<string | undefined> {
  const list = await items;
  if (list.length === 0) {
    vscode.window.showWarningMessage(t('msg.noOptions'));
    return undefined;
  }
  return vscode.window.showQuickPick(list, { placeHolder });
}

async function pickItem<T extends vscode.QuickPickItem>(
  items: T[] | Thenable<T[]>,
  placeHolder: string,
): Promise<T | undefined> {
  const list = await items;
  if (list.length === 0) {
    vscode.window.showWarningMessage(t('msg.noOptions'));
    return undefined;
  }
  return vscode.window.showQuickPick(list, { placeHolder });
}

const BTN = {
  get connect() { return t('btn.connect'); },
  get cancel()  { return t('btn.cancel'); },
  get flash()   { return t('action.flash'); },
  get del()     { return t('action.delete'); },
};

export class ActionHandlers {
  private skillProvider: vscode.Disposable;

  constructor(
    private readonly context: vscode.ExtensionContext,
    private readonly getApi: () => ApiClient,
    private readonly backend: BackendManager,
    private readonly tasks: TaskTerminalManager,
  ) {
    this.skillProvider = vscode.workspace.registerTextDocumentContentProvider('qpi-skill', {
      provideTextDocumentContent: async (uri) => {
        try {
          const doc = await this.getApi().skill(uri.path);
          return `# ${doc.name}\n\n${doc.content}`;
        } catch (e) {
          return t('msg.skillReadFailed', { err: e instanceof Error ? e.message : String(e) });
        }
      },
    });
    context.subscriptions.push(this.skillProvider);
  }

  private async ensureBackend(): Promise<boolean> {
    try {
      await this.backend.start();
      return true;
    } catch (e) {
      vscode.window.showErrorMessage(t('msg.backendFailFull', { err: (e as Error).message }));
      return false;
    }
  }

  /** 未连接设备时提示先连接; 点"连接"会执行向导, 完成后再校验 */
  private async ensureConnected(): Promise<boolean> {
    if (!(await this.ensureBackend())) { return false; }
    try {
      const st = await this.getApi().status();
      // adb 不可用不阻断: 本方法也被编译等非设备操作复用, 设备操作会由后端给出准确报错
      if (st.connected) { await this.ensureAdb(); return true; }
    } catch { /* ignore */ }
    const ret = await vscode.window.showWarningMessage(t('msg.connectPrompt'), BTN.connect, BTN.cancel);
    if (ret !== BTN.connect) { return false; }
    await this.connect();
    try {
      const st2 = await this.getApi().status();
      if (!st2.connected) { return false; }
      await this.ensureAdb();
      return true;
    } catch {
      return false;
    }
  }

  /** 确保本机 adb 可用: 本机设备缺 adb 且宿主是原生 Linux 时自动安装
   *  (有 adb 不装; 仅 sudo 需要密码时才弹密码框; 其余宿主只记日志不打扰)。
   *  返回值表示 adb 最终是否可用; 调用点不以此阻断 (设备操作失败时后端会给出准确提示)。 */
  private async ensureAdb(): Promise<boolean> {
    if (adbEnvDone) { return true; }
    let info: { found: boolean; installable: boolean; need_sudo: boolean; reason: string };
    try {
      info = await this.getApi().adbEnvCheck();
    } catch {
      return true; // 探测失败不阻塞: 交给后端在真正执行时给出准确报错
    }
    if (info.found) { adbEnvDone = true; return true; }
    if (!info.installable) {
      // Windows/WSL (内置 adb.exe 兜底) / macOS / 远端设备: 不代为安装, 只记日志
      adbEnvDone = true;
      if (info.reason) { output.log(`${t('msg.adbNotAvailable')} ${info.reason}`); }
      return true;
    }
    // 原生 Linux 缺 adb: 自动安装。标记先置位, 任何一次尝试后都不再重复探测/安装。
    adbEnvDone = true;
    let pwd = '';
    if (info.need_sudo) {
      const v = await vscode.window.showInputBox({
        prompt: t('prompt.sudoPassword'),
        password: true,
        ignoreFocusOut: true,
        placeHolder: 'sudo',
      });
      if (!v) {
        vscode.window.showWarningMessage(t('msg.adbNotAvailable'));
        return false;
      }
      pwd = v;
    }
    await this.runAdbInstall(pwd);
    // 装完复检: 后端重新解析 adb 路径 (失败时后端已缓存被清空)
    try {
      const after = await this.getApi().adbWhich(true);
      if (after.path) {
        vscode.window.showInformationMessage(t('msg.adbInstalled', { path: after.path }));
        return true;
      }
    } catch { /* ignore, 下面统一提示失败 */ }
    vscode.window.showWarningMessage(t('msg.adbInstallFailed'));
    return false;
  }

  /** 运行 adb 安装任务并等待其结束; 任务未起来或超时则不阻塞后续流程。 */
  private async runAdbInstall(pwd: string): Promise<void> {
    let finish: () => void = () => { /* set below */ };
    const done = new Promise<void>((r) => { finish = r; });
    let timer: ReturnType<typeof setTimeout> | undefined;
    const timeout = new Promise<void>((r) => { timer = setTimeout(() => r(), ADB_INSTALL_WAIT_MS); });
    try {
      const started = await this.tasks.run(
        t('task.installAdb'),
        (api) => api.adbEnvSetup(pwd),
        () => finish(),
      );
      if (!started) { return; }
      await Promise.race([done, timeout]);
    } finally {
      if (timer) { clearTimeout(timer); }
    }
  }

  // ---------- 连接 ----------
  async connect(): Promise<void> {
    if (!(await this.ensureBackend())) { return; }
    resetAdbEnvDone(); // 重新连接: 宿主/设备位置可能变化, 重新探测 adb
    const last = this.context.workspaceState.get<ConnConfig>('qpiSdk.connection') ?? undefined;
    const result = await ConnectPanel.open(this.context.extensionUri, last);
    if (!result) { return; } // 用户取消
    const conn = result.conn;

    let connected = false;
    await vscode.window.withProgress({ location: vscode.ProgressLocation.Notification, title: t('msg.connecting'), cancellable: false }, async () => {
      try {
        await this.getApi().connect(conn);
        await this.context.workspaceState.update('qpiSdk.connection', conn);
        connected = true;
        vscode.window.showInformationMessage(t('msg.connected', { mode: conn.mode, path: conn.sdk_root }));
      } catch (e) {
        // 连接失败: 重新打开面板并带上已填内容, 方便修改
        vscode.window.showErrorMessage(t('msg.connectFailed', { err: (e as Error).message }));
        const retry = await ConnectPanel.open(this.context.extensionUri, conn);
        if (retry) {
          await this.connectWithConfig(retry.conn);
        }
      }
    });
    // 连接成功后检查 SDK 是否落后远端 (联网; 放到进度条结束后, 避免提示叠加)
    if (connected) {
      await this.ensureAdb();
      await this.checkSdkUpdates(false);
    }
  }

  private async connectWithConfig(conn: ConnConfig): Promise<void> {
    resetAdbEnvDone(); // 重新连接: 宿主/设备位置可能变化, 重新探测 adb
    let connected = false;
    await vscode.window.withProgress({ location: vscode.ProgressLocation.Notification, title: t('msg.connecting'), cancellable: false }, async () => {
      try {
        await this.getApi().connect(conn);
        await this.context.workspaceState.update('qpiSdk.connection', conn);
        connected = true;
        vscode.window.showInformationMessage(t('msg.connected', { mode: conn.mode, path: conn.sdk_root }));
      } catch (e) {
        vscode.window.showErrorMessage(t('msg.connectFailed', { err: (e as Error).message }));
        const retry = await ConnectPanel.open(this.context.extensionUri, conn);
        if (retry) { await this.connectWithConfig(retry.conn); }
      }
    });
    if (connected) {
      await this.ensureAdb();
      await this.checkSdkUpdates(false);
    }
  }

  /** 静默重连: 用上次保存的配置自动 connect, 不弹任何 UI; 失败或无记录时直接返回 false */
  async tryReconnect(): Promise<boolean> {
    if (!this.backend.isRunning()) { return false; }
    const last = this.context.workspaceState.get<ConnConfig>('qpiSdk.connection');
    if (!last) { return false; }
    try {
      await this.getApi().connect(last);
      resetAdbEnvDone();
      // 后台检查版本 (silent): 只在真的落后时才提示, 避免每次开 IDE 都弹窗
      void this.checkSdkUpdates(true);
      return true;
    } catch {
      return false;
    }
  }

  // ---------- SDK 版本检查 ----------
  /** 读取已连接 SDK 的 git 状态并与远端比较。
   *  silent=true (自动重连) 时只在"落后"或"读不到远端"时提示, "已是最新"只写日志。 */
  private async checkSdkUpdates(silent = false): Promise<void> {
    let g: SdkGitInfo;
    try {
      g = await this.getApi().sdkGit(true);
    } catch (e) {
      output.log(t('msg.sdkCheckFailed', { err: (e as Error).message }));
      return;
    }
    if (!g.is_repo || !g.has_upstream) {
      output.log(`${t('msg.sdkNotRepo')} ${g.error || ''}`.trim());
      if (!silent && !g.is_repo) { vscode.window.showWarningMessage(t('msg.sdkNotRepo')); }
      return;
    }
    if (!g.fetch_ok) {
      output.log(t('msg.sdkFetchFailed'));
      if (!silent) { vscode.window.showWarningMessage(t('msg.sdkFetchFailed')); }
      return;
    }
    if (!g.up_to_date) {
      const msg = t('msg.sdkOutdated', { behind: String(g.behind), branch: g.branch || g.upstream });
      output.log(msg);
      const ret = await vscode.window.showWarningMessage(msg, t('action.viewSdkInfo'));
      if (ret) { await this.sdkInfo(); }
      return;
    }
    const ok = t('msg.sdkUpToDate', { branch: g.branch || g.upstream, commit: g.commit });
    output.log(ok);
    if (!silent) { vscode.window.showInformationMessage(ok); }
  }

  /** SDK 信息面板 (分支 / 落后远端多少个版本 / 提交记录); 未连接时提示先连接 */
  async sdkInfo(): Promise<void> {
    if (!(await this.ensureBackend())) { return; }
    try {
      const st = await this.getApi().status();
      if (!st.connected) {
        const ret = await vscode.window.showWarningMessage(t('msg.sdkNotConnected'), BTN.connect);
        if (ret === BTN.connect) { await this.connect(); }
        return;
      }
    } catch (e) {
      vscode.window.showErrorMessage(t('act.qpiErr', { err: (e as Error).message }));
      return;
    }
    SdkInfoPanel.open(this.context.extensionUri, () => this.getApi());
  }

  async disconnect(): Promise<void> {
    if (!(await this.ensureBackend())) { return; }
    await this.getApi().disconnect();
    vscode.window.showInformationMessage(t('msg.disconnected'));
  }

  async refreshDevices(): Promise<void> {
    if (!(await this.ensureConnected())) { return; }
    try {
      const [dev, ports] = await Promise.all([this.getApi().devices(), this.getApi().ports()]);
      const lines = dev.devices.map((d) => `${d.serial}  [${d.state}]`);
      const portLines = ports.ports.map((p) => `${p.device}  ${p.desc}${p.adb_serial ? '  (adb:' + p.adb_serial + ')' : ''}`);
      const all = [...lines, ...portLines];
      if (all.length === 0) {
        vscode.window.showWarningMessage(t('msg.noDevices'));
        return;
      }
      await pick(all, t('prompt.devicesFound'));
    } catch (e) {
      vscode.window.showErrorMessage(t('msg.refreshDevFail', { err: (e as Error).message }));
    }
  }

  // ---------- 编译 ----------
  /** 选择要编译的应用工程目录 (app 编译需要指定应用目录) */
  private async pickAppDir(): Promise<string | undefined> {
    try {
      const pr = await this.getApi().projects();
      if (pr.projects.length === 0) {
        vscode.window.showInformationMessage(t('msg.noProjects'));
        return undefined;
      }
      const pick = await vscode.window.showQuickPick(
        pr.projects.map((p) => ({ label: p.name, description: p.dir, dir: p.dir })),
        { placeHolder: t('prompt.pickApp') },
      );
      return pick?.dir;
    } catch (e) {
      vscode.window.showErrorMessage(t('msg.projectsLoadFailed', { err: (e as Error).message }));
      return undefined;
    }
  }

  build = (mode: string, title: string) => async (): Promise<void> => {
    if (!(await this.ensureConnected())) { return; }
    // Yocto SDK: 编译入口是 source quectel_build/compile/build.sh 后的 BitBake 函数。
    // BitBake 在用户态运行, 不需要 sudo; 编译一律在 SDK 所在主机上原生执行。
    const plat = await this.getPlatform();
    if (plat?.sdk_type === 'yocto') {
      // 并行度与 buildconfig 参数都来自「配置构建环境」面板的保存值:
      // 后端会在同一会话内先 buildconfig 再编译 (与 SDK README 流程一致)
      const threads = getYoctoThreads(this.context);
      const yocto = yoctoBuildPayload(this.context);
      await this.tasks.run(title, (api) => api.build(mode, '', '', threads, yocto));
      return;
    }
    let arg = '';
    if (mode === 'app') {
      const dir = await this.pickAppDir();
      if (!dir) { return; }
      arg = dir;
    }
    // 打包 rootfs (mode = all/rootfs) 需要 root 保真复制 base 属主。采集密码的
    // 逻辑见本文件末尾的 ensureSudoPassword, 与面板 Webview 的入口共用一份缓存。
    const pwd = await ensureSudoPassword(this.getApi(), mode);
    if (pwd === undefined) { return; } // 用户在密码框取消
    await this.tasks.run(
      title,
      (api) => api.build(mode, arg, pwd, getYoctoThreads(this.context),
        yoctoBuildPayload(this.context)),
      // 密码错: 清缓存并提示, 用户重跑时会重新询问
      (_code, sawSudoError) => { if (sawSudoError) { reportSudoError(); } },
    );
  };

  /** 一键配置构建环境: 检查/安装 SDK 所在环境的 Docker, 再在容器内执行 build.sh setenv。
    编译已统一放进 Docker 容器 (依赖全部预置在镜像里, 与宿主环境无关)。
    Yocto SDK (Pi-H1-Linux-SDK): 走 buildconfig 向导 (项目 ID + 固件类型)。 */
  setupBuildEnv = async (): Promise<void> => {
    if (!(await this.ensureConnected())) { return; }
    // Yocto SDK: 配置构建环境 = 打开配置面板 (下拉/输入 + docker 环境检测自动配置 + buildconfig)
    const plat = await this.getPlatform();
    if (plat?.sdk_type === 'yocto') {
      YoctoConfigPanel.open(this.context.extensionUri, this.getApi, this.tasks, this.context);
      return;
    }
    const api = this.getApi();
    // 1) 探测 SDK 所在环境的 Docker 状态 (env_dir / 安装 / daemon / 镜像)
    let st: { env_dir: boolean; docker_installed: boolean; daemon_ok: boolean; image_present: boolean; host_os: string };
    try {
      st = await api.dockerStatus();
    } catch (e) {
      vscode.window.showErrorMessage(t('docker.statusFail', { err: (e as Error).message }));
      return;
    }
    if (!st.env_dir) {
      vscode.window.showWarningMessage(t('docker.noEnvDir'));
      return;
    }
    const dockerReady = st.docker_installed && st.daemon_ok && st.image_present;
    // 2) 未就绪: 安装 Docker / 启动 daemon / 构建镜像需要 sudo 密码 (复用会话缓存)。
    //    macOS 走 Homebrew + colima (无需 sudo), 跳过密码提示, 交给 dockerSetup 自动安装。
    let pwd = '';
    if (!dockerReady && st.host_os !== 'darwin') {
      const p = await ensureSudoPassword(api, 'docker');
      if (p === undefined) { return; } // 用户取消
      pwd = p;
    }
    // 3) qemu binfmt 未注册时先补上 (Linux/WSL 交叉工具链需要): WSL 未启用 systemd
    //    时 binfmt-support.service 不运行, 包虽已装但内核态未注册, 交叉编译会直接失败。
    let binfmtMissing: string[] = [];
    try {
      binfmtMissing = (await api.envBinfmtCheck()).missing ?? [];
    } catch { /* 探测失败不阻塞, 交给 SDK 脚本处理 */ }
    if (binfmtMissing.length > 0) {
      if (!pwd) {
        const p = await vscode.window.showInputBox({
          prompt: t('prompt.sudoPassword'),
          password: true,
          ignoreFocusOut: true,
          placeHolder: 'sudo',
        });
        if (!p) { return; }
        pwd = p;
      }
      await this.tasks.run(t('task.enableBinfmt'), (a) => a.envBinfmt(pwd));
    }
    // 4) Docker 未就绪: 安装/启动 daemon/构建镜像 (幂等)
    if (!dockerReady) {
      // 必须等镜像真正构建完成再执行下一步: 任务终端是异步打开的,
      // 否则 envSetup 会在镜像尚未构建好时就 docker run, 报"编译镜像缺失"。
      let code: number | null = null;
      await new Promise<void>((resolve) => {
        void this.tasks.run(
          t('task.dockerSetup'),
          (a) => a.dockerSetup(pwd),
          (c) => { code = c; resolve(); },
        );
      });
      if (code !== 0) {
        vscode.window.showErrorMessage(t('docker.setupFailed'));
        return;
      }
    }
    // 5) 容器内执行 SDK setenv (依赖补齐 / 底包下载 / 环境校验)
    await this.tasks.run(t('task.setupBuildEnv'), (a) => a.envSetup(pwd));
  };

  // ---------- Docker (Manage Docker: 手动构建备用编译镜像) ----------
  /** 检查/安装 Docker 并构建容器镜像 (须先连接 SDK; 下载/构建过程在任务终端显示进度)。
      状态: ① docker_env 目录存在? ② docker 已安装? ③ daemon 可用? ④ 镜像已构建?
      未就绪时自动执行安装/构建, 需要 sudo 权限时提示输入密码。 */
  async manageDocker(): Promise<void> {
    if (!(await this.ensureConnected())) { return; }
    let st: { env_dir: boolean; docker_installed: boolean; daemon_ok: boolean; image_present: boolean; error: string };
    try {
      st = await this.getApi().dockerStatus();
    } catch (e) {
      vscode.window.showErrorMessage(t('docker.statusFail', { err: (e as Error).message }));
      return;
    }
    if (!st.env_dir) {
      vscode.window.showWarningMessage(t('docker.noEnvDir'));
      return;
    }
    if (st.docker_installed && st.daemon_ok && st.image_present) {
      vscode.window.showInformationMessage(t('docker.ready'));
      return;
    }
    // 需要安装 Docker / 启动 daemon / 构建镜像: 探测 sudo 是否需要密码
    // (mode="docker" 时后端按免密探测结果给出 needed)
    let needPwd = true;
    try {
      const sudo = await this.getApi().sudoCheck('docker');
      if (sudo.docker === true && !sudo.needed) { needPwd = false; }
    } catch { /* 探测失败按需要密码处理 */ }
    if (needPwd) {
      const pwd = await vscode.window.showInputBox({
        prompt: t('docker.sudoPrompt'),
        password: true,
        ignoreFocusOut: true,
        placeHolder: 'sudo',
      });
      if (pwd === undefined) { return; } // 用户取消
      await this.tasks.run(t('task.dockerSetup'), (api) => api.dockerSetup(pwd));
      return;
    }
    await this.tasks.run(t('task.dockerSetup'), (api) => api.dockerSetup(''));
  }

  // ---------- 烧录 ----------
  /** 读取当前连接 SDK 的平台标识 (后端读 tools/VERSION.txt; 未连接/失败返回 undefined) */
  private async getPlatform(): Promise<SdkPlatform | undefined> {
    try {
      const st = await this.getApi().status();
      return st.platform ?? undefined;
    } catch {
      return undefined;
    }
  }

  /** 是否 Qualcomm 平台 (H1): 烧录走 SDK 自带脚本, 进下载模式为 EDL(9008) */
  private static isQualcomm(plat?: SdkPlatform): boolean {
    return plat?.family === 'qualcomm';
  }

  async rebootLoader(): Promise<void> {
    if (!(await this.ensureConnected())) { return; }
    // 进下载模式按平台命名: M2 = Loader, H1 = EDL(9008); 实际命令由后端按平台决定
    const plat = await this.getPlatform();
    const title = ActionHandlers.isQualcomm(plat)
      ? t('task.rebootEdlQualcomm', { board: plat?.board || 'H1' })
      : t('task.rebootLoader');
    await this.tasks.run(title, (api) => api.rebootLoader());
  }

  /** 烧录工具提示: 由后端 hint_key/hint_vars 渲染, 架构不匹配时追加警告 */
  private toolHint(tool: { hint_key?: string; hint_vars?: Record<string, string>; arch_warn?: [string, string] }): string {
    let s = tool.hint_key ? t(tool.hint_key, tool.hint_vars || {}) : '';
    if (tool.arch_warn) {
      s += t('flash.hint.archMismatch', { arch: tool.arch_warn[0], mach: tool.arch_warn[1] });
    }
    return s;
  }

  /** 烧录工具选择项: label 为翻译后显示文本, raw 为后端解析用的原始工具名 */
  private async pickFlashTool(): Promise<string | undefined> {
    const { tools } = await this.getApi().flashTools();
    const found = tools.filter((t) => t.found);
    const items: Array<{ label: string; description?: string; raw: string }> =
      (found.length > 0 ? found : tools).map((t) => ({
        label: t.name,
        description: found.length > 0 ? undefined : this.toolHint(t),
        raw: t.name,
      }));
    const sel = await pickItem(
      items,
      found.length > 0 ? t('prompt.pickFlashTool') : t('prompt.pickFlashToolManual'),
    );
    return sel?.raw;
  }

  async flash(): Promise<void> {
    if (!(await this.ensureConnected())) { return; }
    const plat = await this.getPlatform();
    if (ActionHandlers.isQualcomm(plat)) {
      // Qualcomm (H1): 全量烧录由 SDK 自带脚本完成 —— 没有工具可选, 也不用 update.img
      const board = plat?.board || 'H1';
      const confirm = await vscode.window.showWarningMessage(
        t('prompt.flashConfirmH1', { board, proto: plat?.flash_protocol || 'EDL' }),
        BTN.flash, BTN.cancel);
      if (confirm !== BTN.flash) { return; }
      // 远端设备模式下烧录脚本内部 sudo 需要密码 (免密时返回 '' 不打扰)
      const pwd = await ensureSudoPassword(this.getApi(), 'flash');
      if (pwd === undefined) { return; }
      await this.tasks.run(t('task.flashAllH1', { board }), (api) => api.flash('', '', pwd || ''));
      return;
    }
    const tool = await this.pickFlashTool();
    if (!tool) { return; }
    const confirm = await vscode.window.showWarningMessage(t('prompt.flashConfirm'), BTN.flash, BTN.cancel);
    if (confirm !== BTN.flash) { return; }
    await this.tasks.run(t('task.flashAll', { tool }), (api) => api.flash(tool));
  }

  async flashBoot(): Promise<void> {
    if (!(await this.ensureConnected())) { return; }
    const plat = await this.getPlatform();
    if (ActionHandlers.isQualcomm(plat)) {
      // H1 的 SDK 脚本只做全盘烧录 (所有 LUN), 不支持单独烧某个分区
      vscode.window.showWarningMessage(t('msg.h1NoPartitionFlash', { board: plat?.board || 'H1' }));
      return;
    }
    const tool = await this.pickFlashTool();
    if (!tool) { return; }
    await this.tasks.run(t('task.flashBoot', { tool }), (api) => api.flashPartition(tool, 'boot'));
  }

  /** Yocto SDK 专属: 烧录编译产物到设备分区。
      - 'both' (默认, 侧边栏「烧录内核」): 依次部署 dtb.bin + efi.bin
      仅 yocto 平台可用; 产物缺失时后端给出明确提示。 */
  private async deployYocto(part: 'boot' | 'kernel' | 'both'): Promise<void> {
    if (!(await this.ensureConnected())) { return; }
    const plat = await this.getPlatform();
    if (plat?.sdk_type !== 'yocto') {
      vscode.window.showWarningMessage(t('yocto.deployOnlyYocto'));
      return;
    }
    const title = part === 'both' ? t('task.deployKernel') : (part === 'kernel' ? t('task.deployKernel') : t('task.deployBoot'));
    await this.tasks.run(title, (api) => api.yoctoDeploy(part));
  }

  /** 烧录内核 (Boot + 内核): 一次部署 dtb.bin + efi.bin */
  deployYoctoKernel = async (): Promise<void> => this.deployYocto('both');

  async deploy(): Promise<void> {
    if (!(await this.ensureConnected())) { return; }
    const uris = await vscode.window.showOpenDialog({ title: t('prompt.deployFile'), canSelectMany: false });
    if (!uris || uris.length === 0) { return; }
    const source = uris[0].fsPath;
    const target = await input(t('prompt.deployTarget'), '/userdata/' + path.basename(source));
    if (target === undefined) { return; }
    await this.tasks.run(t('task.deploy', { name: path.basename(source) }), (api) => api.deploy(source, target));
  }

  // ---------- 设备 ----------
  async deviceInfo(): Promise<void> {
    // 设备信息是只读诊断: 仅需后端与 adb 设备 (无需连接 SDK 编译环境)
    if (!(await this.ensureBackend())) { return; }
    DeviceInfoPanel.open(this.context.extensionUri, () => this.getApi());
  }

  async adb(): Promise<void> {
    if (!(await this.ensureConnected())) { return; }
    // 复用已打开的 adb 终端; 否则新建并直接进入设备 adb shell 环境
    const existing = this.terminalByName(t('term.adb'));
    if (existing) { existing.show(true); return; }

    let adbPath = '';
    try {
      adbPath = (await this.getApi().adbWhich()).path || '';
    } catch { /* 拿不到 adb 路径也不阻塞打开终端 */ }

    // 设备连接位置: 远端设备 (ssh + 板子接编译机) 时, adb shell 必须经 ssh
    // 在编译机上执行, 不能在本机终端直接跑 adb (本机连不到编译机上的板子)。
    let remoteDev = false;
    let conn: ConnConfig | undefined;
    try {
      const st = await this.getApi().status();
      conn = st.conn || undefined;
      remoteDev = !!(conn && conn.mode === 'ssh' && conn.device_location === 'remote');
    } catch { /* 拿不到状态就按本机处理 */ }

    if (remoteDev && conn) {
      const sshArgs = ['-o', 'StrictHostKeyChecking=no', '-t', `-p ${conn.port || 22}`];
      if (conn.key_path) { sshArgs.push('-i', `"${conn.key_path}"`); }
      // adbPath 此时是后端在编译机解析出的远端 adb 路径 (如 /usr/bin/adb)
      const shellCmd = adbPath ? `"${adbPath}" shell` : 'adb shell';
      const term = vscode.window.createTerminal({
        name: t('term.adb'),
        cwd: vscode.workspace.workspaceFolders?.[0]?.uri.fsPath,
      });
      term.show(true);
      const banner = `${t('term.adbBanner')} | ${conn.user}@${conn.host}`;
      term.sendText(`echo '${banner}'`);
      // ssh 登录编译机后直接在远端执行 adb shell (交互式); 密码需在终端输入一次
      term.sendText(`ssh ${sshArgs.join(' ')} ${conn.user}@${conn.host} "${shellCmd}"`);
      return;
    }

    const adbWin = this.adbWinPath(adbPath);
    const posixAdb = process.platform !== 'win32' && adbPath.startsWith('/') ? adbPath : '';

    const env: Record<string, string> = {};
    for (const k in process.env) {
      const v = process.env[k];
      if (v !== undefined) { env[k] = v; }
    }
    env.MSYS_NO_PATHCONV = '1';
    env.MSYS2_ARG_CONV_EXCL = '*';
    if (adbWin) {
      env.PATH = adbWin.replace(/[\\/][^\\/]+$/, '') + path.delimiter + (env.PATH || '');
    }

    const term = vscode.window.createTerminal({
      name: t('term.adb'),
      cwd: vscode.workspace.workspaceFolders?.[0]?.uri.fsPath,
      env,
    });
    term.show(true);
    let banner = t('term.adbBanner');
    if (posixAdb) { banner += ` | adb: ${posixAdb}`; }
    else if (adbWin) { banner += ` | adb: ${adbWin}`; }
    term.sendText(`echo '${banner}'`);
    // 直接进入设备 adb shell: 之后输入 ls / cat 等即在本机 adb shell 内执行
    term.sendText(posixAdb ? `"${posixAdb}" shell` : 'adb shell');
  }

  /** 把后端返回的 adb 路径转成 Windows 终端 (PowerShell/cmd) 可用路径。
      git-bash 风格 /c/Users/... -> C:\Users\...; Windows 路径仅规整斜杠;
      无法识别时返回空串 (不污染 PATH)。 */
  private adbWinPath(adbPath: string): string {
    if (!adbPath) { return ''; }
    const m = /^\/[a-zA-Z]\/(.*)$/.exec(adbPath);
    if (m) {
      return m[0][1].toUpperCase() + ':\\' + m[1].replace(/\//g, '\\');
    }
    return /^[A-Za-z]:/.test(adbPath) ? adbPath.replace(/\//g, '\\') : '';
  }

  async dmesg(): Promise<void> {
    if (!(await this.ensureConnected())) { return; }
    const follow = await pick([t('prompt.logSnapshot'), t('prompt.logFollow')], t('prompt.dmesgMode'));
    if (!follow) { return; }
    const filter = (await input(t('prompt.filter'), '')) ?? '';
    await this.tasks.run(`dmesg${follow === t('prompt.logFollow') ? ' -f' : ''}`, (api) =>
      api.dmesg(filter, follow === t('prompt.logFollow')));
  }

  async journalctl(): Promise<void> {
    if (!(await this.ensureConnected())) { return; }
    const follow = await pick([t('prompt.logSnapshot'), t('prompt.logFollow')], t('prompt.journalctlMode'));
    if (!follow) { return; }
    const filter = (await input(t('prompt.filter'), '')) ?? '';
    await this.tasks.run(`journalctl${follow === t('prompt.logFollow') ? ' -f' : ''}`, (api) =>
      api.journalctl(filter, follow === t('prompt.logFollow')));
  }

  async reboot(): Promise<void> {
    if (!(await this.ensureConnected())) { return; }
    // 仅执行普通重启 (adb shell reboot)。
    // 进入下载模式请用「进 Loader」按钮: 后端按平台自动选 reboot loader (M2) / reboot edl (H1)。
    await this.tasks.run(t('tile.reboot'), (api) => api.reboot(false));
  }

  // ---------- AT / 音频 / 性能 ----------
  async at(): Promise<void> {
    if (!(await this.ensureConnected())) { return; }
    // 打开 AT 指令面板: 串口下拉选择 + 指令输入 + 发送 + 日志区
    ATPanel.open(this.context.extensionUri, () => this.getApi());
  }

  // ---------- 设备树 ----------
  async deviceTree(): Promise<void> {
    if (!(await this.ensureConnected())) { return; }
    // 打开设备树配置面板: 类似连接 SDK 的独立弹窗
    DeviceTreePanel.open(this.context.extensionUri, () => this.getApi());
  }

  async audioCollect(): Promise<void> {
    if (!(await this.ensureConnected())) { return; }
    await this.tasks.run(t('task.audioCollect'), (api) => api.audioCollect());
  }

  async tinymix(): Promise<void> {
    if (!(await this.ensureConnected())) { return; }
    const args = await input(t('prompt.tinymixArgs'), 'controls');
    if (args === undefined) { return; }
    await this.tasks.run(`tinymix ${args}`, (api) => api.tinymix(args));
  }

  async aplay(): Promise<void> {
    if (!(await this.ensureConnected())) { return; }
    const file = await input(t('prompt.audioFile'), '/tmp/test.wav');
    if (file === undefined) { return; }
    await this.tasks.run(t('tile.aplay'), (api) => api.aplay(file));
  }

  async videoPlay(): Promise<void> {
    if (!(await this.ensureConnected())) { return; }
    const file = await input(t('prompt.videoFile'), '/tmp/test.mp4');
    if (file === undefined) { return; }
    await this.tasks.run(t('tile.videoPlay'), (api) => api.playVideo(file));
  }

  async screenRecord(): Promise<void> {
    if (!(await this.ensureConnected())) { return; }
    const secs = await input(t('prompt.recDuration'), '10');
    if (secs === undefined) { return; }
    await this.tasks.run(t('tile.screenRecord'), (api) => api.screenRecord(secs));
  }

  screenshot = this.task(t('tile.screenshot'), (api) => api.screenshot());

  async perfMonitor(): Promise<void> {
    // 性能监控是只读诊断: 仅需后端与 adb 设备 (无需连接 SDK 编译环境),
    // 打开独立面板后由面板自身轮询 /perf/sample 实时刷新图表。
    if (!(await this.ensureBackend())) { return; }
    PerfPanel.open(this.context.extensionUri, () => this.getApi());
  }

  /** 按名称复用终端: 已存在则聚焦, 不重复创建, 避免终端堆积 */
  private terminalByName(name: string): vscode.Terminal | undefined {
    return vscode.window.terminals.find((t) => t.name === name);
  }

  /** 为 SSH 自动登录写 askpass 辅助文件 (密码 pwd.txt + askpass.cmd), 返回 askpass
      脚本路径; 无密码或写文件失败时返回 undefined (退回手动输入)。
      askpass.cmd 用 type 原样输出密码文件, 避免 cmd 对 & % ^ 等特殊字符的转义问题。 */
  private writeSshAskpass(password?: string): string | undefined {
    if (!password) { return undefined; }
    try {
      const dir = path.join(this.context.globalStorageUri.fsPath, 'ssh_askpass');
      fs.mkdirSync(dir, { recursive: true });
      const pwdFile = path.join(dir, 'pwd.txt');
      const askpassFile = path.join(dir, 'askpass.cmd');
      fs.writeFileSync(pwdFile, password, 'utf8'); // 无换行: type 原样输出即密码
      fs.writeFileSync(askpassFile, `@echo off\r\ntype "${pwdFile}"\r\n`, 'utf8');
      return askpassFile;
    } catch {
      return undefined; // 磁盘/权限异常时退回手动输入密码
    }
  }

 
  async execShell(): Promise<void> {
    if (!(await this.ensureConnected())) { return; }
    const existing = this.terminalByName(t('term.shell'));
    if (existing) { existing.show(true); return; }

    const conn = this.context.workspaceState.get<ConnConfig>('qpiSdk.connection');
    if (!conn || !conn.sdk_root) {
      vscode.window.showWarningMessage(t('msg.sdkConfigMissing'));
      return;
    }

    const env: Record<string, string> = {};
    for (const k in process.env) {
      const v = process.env[k];
      if (v !== undefined) { env[k] = v; }
    }
    env.MSYS_NO_PATHCONV = '1';
    env.MSYS2_ARG_CONV_EXCL = '*';

    const opts: vscode.TerminalOptions = { name: t('term.shell'), env };
    let init = '';
    if (conn.mode === 'ssh') {
      // 引号策略: 远程命令整体用双引号包住 (本机 PowerShell/cmd 均按一个参数解析,
      // && 不会在本地被解释); 路径在远端 bash 里用单引号包裹 (容纳空格/特殊字符)。
      const sshArgs = ['-t'];
      if (conn.port && conn.port !== 22) { sshArgs.push('-p', String(conn.port)); }
      if (conn.key_path) { sshArgs.push('-i', `"${conn.key_path}"`); }
      const askpass = this.writeSshAskpass(conn.password);
      if (askpass) {
        env.SSH_ASKPASS = askpass;
        env.SSH_ASKPASS_REQUIRE = 'force';
      }
      const remote = `cd '${conn.sdk_root}' && exec bash -l`;
      init = `ssh ${sshArgs.join(' ')} ${conn.user}@${conn.host} "${remote}"`;
    } else {
      const m = /^\\\\wsl(?:\.localhost|\$)\\([^\\]+)\\(.+)$/i.exec(conn.sdk_root);
      if (m) {
        // WSL: 进入发行版并切到 SDK 目录, 之后每条命令都在 SDK 环境执行
        init = `wsl.exe -d ${m[1]} --cd /${m[2].replace(/\\/g, '/')}`;
      } else {
        // 本机普通路径: 默认 shell + cwd=SDK 根目录
        opts.cwd = conn.sdk_root;
      }
    }

    const term = vscode.window.createTerminal(opts);
    term.show(true);
    term.sendText(`echo '${t('term.shellBanner', { path: conn.sdk_root })}'`);
    if (init) { term.sendText(init); }
  }

  // ---------- SKILL 文档 ----------
  async skills(): Promise<void> {
    if (!(await this.ensureConnected())) { return; }
    try {
      const { skills } = await this.getApi().skills();
      if (skills.length === 0) {
        vscode.window.showWarningMessage(t('msg.noSkill'));
        return;
      }
      const name = await pick(skills.map((s) => s.name), t('prompt.skillSelect'));
      if (!name) { return; }
      const uri = vscode.Uri.parse(`qpi-skill:${name}`);
      const doc = await vscode.workspace.openTextDocument(uri);
      await vscode.window.showTextDocument(doc, { preview: true });
    } catch (e) {
      vscode.window.showErrorMessage(t('msg.skillFail', { err: (e as Error).message }));
    }
  }

  // ---------- 工程创建向导 (QuickPick 多步, 替代 Webview 工程页) ----------
  async createProject(): Promise<void> {
    if (!(await this.ensureConnected())) { return; }
    interface Tpl {
      id: string; name: string; description?: string; tag?: string; category?: string;
      icon?: string; variables?: Record<string, { label: string; type?: string; default?: unknown; hint?: string; choices?: Array<{ value: string; label: string }> }>;
    }
    let tpls: Tpl[];
    try {
      const r = await this.getApi().templates();
      tpls = r.templates as unknown as Tpl[];
    } catch (e) {
      vscode.window.showErrorMessage(t('msg.templatesLoadFailed', { err: (e as Error).message }));
      return;
    }
    if (!tpls || tpls.length === 0) {
      vscode.window.showWarningMessage(t('msg.noTemplates'));
      return;
    }
    const tplPick = await vscode.window.showQuickPick(
      tpls.map((t) => ({
        label: `${t.icon || '$(file-code)'} ${t.name}`,
        description: t.tag || t.category || '',
        detail: t.description || '',
        tpl: t,
      })),
      { placeHolder: t('act.pickTpl'), matchOnDetail: true },
    );
    if (!tplPick) { return; }
    const tpl = tplPick.tpl;

    const variables: Record<string, string> = {};
    for (const [key, v] of Object.entries(tpl.variables ?? {})) {
      if (v.type === 'choice' && v.choices && v.choices.length > 0) {
        const c = await vscode.window.showQuickPick(
          v.choices.map((x) => ({ label: x.label, value: String(x.value) })),
          { placeHolder: v.label },
        );
        if (!c) { return; }
        variables[key] = c.value;
      } else {
        const val = await input(v.label + (v.hint ? ` (${v.hint})` : ''), String(v.default ?? ''));
        if (val === undefined) { return; }
        variables[key] = val;
      }
    }

    const defaultName = `${tpl.id}_demo`;
    const name = await input(t('act.projNamePrompt'), defaultName);
    if (name === undefined) { return; }
    const trimmed = name.trim();
    if (!/^[A-Za-z0-9_\-]{1,64}$/.test(trimmed)) {
      vscode.window.showErrorMessage(t('msg.invalidProjectName'));
      return;
    }
    try {
      const r = await this.getApi().projectCreate(trimmed, tpl.id, variables);
      if (r.ok) {
        vscode.window.showInformationMessage(t('msg.projectCreated', { dir: r.dir ?? '' }));
      } else {
        vscode.window.showErrorMessage(t('msg.projectCreateFailed', { err: r.error || t('act.unknownError') }));
      }
    } catch (e) {
      vscode.window.showErrorMessage(t('msg.projectCreateFailed', { err: (e as Error).message }));
    }
  }

  // ---------- 工程管理 (打开 / 编译 / 删除) ----------
  async manageProjects(): Promise<void> {
    if (!(await this.ensureConnected())) { return; }
    let projects: Array<{ name: string; dir: string; hasBinary: boolean }>;
    let sdkRoot = '';
    let mode: 'local' | 'ssh' = 'local';
    try {
      const [pr, st] = await Promise.all([this.getApi().projects(), this.getApi().status()]);
      projects = pr.projects;
      sdkRoot = st.conn?.sdk_root || '';
      mode = st.conn?.mode === 'ssh' ? 'ssh' : 'local';
    } catch (e) {
      vscode.window.showErrorMessage(t('msg.projectsLoadFailed', { err: (e as Error).message }));
      return;
    }
    if (projects.length === 0) {
      vscode.window.showInformationMessage(t('msg.noProjectsCreate'));
      return;
    }
    const pick2 = await vscode.window.showQuickPick(
      projects.map((p) => ({
        label: `$(folder) ${p.name}${p.hasBinary ? t('act.compiledSuffix') : ''}`,
        description: p.dir,
        proj: p,
      })),
      { placeHolder: t('act.pickProject') },
    );
    if (!pick2) { return; }
    const p = pick2.proj;
    const act = await vscode.window.showQuickPick(
      [
        { label: t('act.openInExplorer'), id: 'open' },
        { label: t('act.buildProject'), id: 'build' },
        { label: t('act.deleteProject'), id: 'del' },
      ],
      { placeHolder: t('act.actForProject', { name: p.name }) },
    );
    if (!act) { return; }
    if (act.id === 'open') {
      this.revealFolder(p.dir, sdkRoot, mode);
    } else if (act.id === 'build') {
      await this.tasks.run(t('act.buildTitle', { name: p.name }), (api) => api.build('app', p.dir));
    } else if (act.id === 'del') {
      const confirm = await vscode.window.showWarningMessage(
        t('act.deleteConfirm', { name: p.name }), { modal: true }, BTN.del);
      if (confirm !== BTN.del) { return; }
      try {
        const r = await this.getApi().projectDelete(p.name);
        if (r.ok) { vscode.window.showInformationMessage(t('msg.projectDeleted')); }
        else { vscode.window.showErrorMessage(t('msg.projectDeleteFailed', { err: r.error || t('act.unknownError') })); }
      } catch (e) {
        vscode.window.showErrorMessage(t('msg.projectDeleteFailed', { err: (e as Error).message }));
      }
    }
  }

  private revealFolder(rel: string, sdkRoot: string, mode: 'local' | 'ssh'): void {
    try {
      if (mode === 'ssh') {
        const conn = this.context.workspaceState.get<ConnConfig>('qpiSdk.connection');
        const host = conn?.host ?? '';
        const abs = rel.startsWith('/') ? rel : path.posix.join(sdkRoot, rel);
        const unc = `\\\\${host}\\share${abs.replace(/\//g, '\\')}`;
        void vscode.commands.executeCommand('revealInExplorer', vscode.Uri.file(unc));
      } else {
        const abs = path.isAbsolute(rel) ? rel : path.join(sdkRoot, rel);
        void vscode.commands.executeCommand('revealInExplorer', vscode.Uri.file(abs));
      }
    } catch (e) {
      vscode.window.showErrorMessage(t('msg.folderOpenFailed', { err: (e as Error).message }));
    }
  }

  // ---------- 环境/仓库引导向导 (QuickPick, 替代 Webview 仓库页) ----------
  async setupEnv(): Promise<void> {
    if (!(await this.ensureConnected())) { return; }
    const repos = this.context.workspaceState.get<Record<string, string>>('qpiSdk.repos') ?? {};
    let comps: Array<{ label: string; rel: string; exists: boolean; url: string }>;
    try {
      const r = await this.getApi().repoStatus(repos);
      comps = r.components;
    } catch (e) {
      vscode.window.showErrorMessage(t('msg.repoStatusFailed', { err: (e as Error).message }));
      return;
    }
    const missing = comps.filter((c) => !c.exists);
    const choice = await vscode.window.showQuickPick(
      [
        ...missing.map((c) => ({
          label: t('act.cloneMissing', { label: c.label }),
          description: c.rel,
          detail: c.url || t('act.needUrl'),
          comp: c,
        })),
        { label: t('act.rescan'), id: 'rescan' as const },
        { label: t('act.cloneCoreTo'), id: 'core' as const },
      ],
      { placeHolder: t('act.setupPlaceholder', { ready: comps.length - missing.length, total: comps.length }) },
    );
    if (!choice) { return; }
    if ('comp' in choice && choice.comp) {
      const c = choice.comp;
      const url = await input(t('act.repoUrlPrompt', { rel: c.rel }), repos[c.rel] || c.url || '');
      if (!url) { return; }
      repos[c.rel] = url.trim();
      await this.context.workspaceState.update('qpiSdk.repos', repos);
      await this.tasks.run(t('act.cloneTask', { label: c.label }), (api) => api.repoClone(c.rel, url.trim()));
    } else if ('id' in choice && choice.id === 'core') {
      const url = await input(t('act.coreUrlPrompt'), repos.core || '');
      if (!url) { return; }
      const dir = await input(t('act.coreDirPrompt'));
      if (!dir) { return; }
      repos.core = url.trim();
      await this.context.workspaceState.update('qpiSdk.repos', repos);
      await this.tasks.run(t('act.cloneCoreTask'), (api) => api.repoCloneCore(url.trim(), dir.trim()));
    } else if ('id' in choice && choice.id === 'rescan') {
      await this.setupEnv();
    }
  }

  // ---------- 后端信息 ----------
  async backendInfo(): Promise<void> {
    if (!(await this.ensureBackend())) { return; }
    try {
      const st = await this.getApi().status();
      const h = this.backend.getHandle();
      vscode.window.showInformationMessage(t('backend.info', {
        host: h?.host ?? '',
        port: h?.port ?? '',
        status: st.connected ? t('backend.connected', { display: st.display }) : t('backend.noDevice'),
      }));
    } catch (e) {
      vscode.window.showErrorMessage(t('act.qpiErr', { err: (e as Error).message }));
    }
  }

  // ---------- SDK 下载 (M2/H1) ----------
  async manageSdks(): Promise<void> {
    if (!(await this.ensureBackend())) { return; }
    const ver = await vscode.window.showQuickPick(
      [
        { label: 'M2', description: t('sdk.versionM2') },
        { label: 'H1', description: t('sdk.versionH1') },
      ],
      { placeHolder: t('sdk.pickVersion') },
    );
    if (!ver) { return; }
    let verKey = ver.label;
    let display = verKey;
    // H1 系列再细分: Simple SDK (QPi-SDK, H1-SDK 分支) / Yocto SDK (Pi-H1-Linux-SDK, master)
    if (verKey === 'H1') {
      const variant = await vscode.window.showQuickPick<vscode.QuickPickItem & { id: string }>(
        [
          { id: 'simple', label: t('sdk.versionH1Simple'), description: '' },
          { id: 'yocto', label: t('sdk.versionH1Yocto'), description: '' },
        ],
        { placeHolder: t('sdk.pickH1Variant') },
      );
      if (!variant) { return; }
      display = variant.label;
      if (variant.id === 'yocto') {
        verKey = 'H1_YOCTO';
      } else {
        verKey = 'H1'; // Simple = 原 H1 SDK, 保存目录仍为 h1, 与旧行为一致
      }
    }

    const lastDir = this.context.workspaceState.get<string>('qpiSdk.lastSdkSaveDir') ?? '';
    const dir = await vscode.window.showInputBox({
      prompt: t('sdk.savePath'),
      value: lastDir,
      placeHolder: t('sdk.savePathPh'),
      ignoreFocusOut: true,
    });
    if (!dir) { return; }
    await this.context.workspaceState.update('qpiSdk.lastSdkSaveDir', dir);

    const src = SDK_SOURCES[verKey];
    if (!src || (!src.repo && !src.archive)) {
      vscode.window.showWarningMessage(t('sdk.todo', { ver: display }));
      return;
    }

    // 在独立终端中下载: 实时看 git clone 进度, 关闭终端即终止下载
    vscode.window.showInformationMessage(t('sdk.downloading', { ver: display }));
    await this.tasks.run(t('act.downloadSdkTask', { ver: display }), (api) =>
      api.sdkDownload(verKey, src.repo || '', src.branch || '', src.archive || '', dir.trim()));
  }

  // ---------- 文档 / 示例 (官网跳转) ----------
  /** 在系统默认浏览器中打开外部链接, 失败时提示。 */
  private async openUrl(url: string): Promise<void> {
    try {
      await vscode.env.openExternal(vscode.Uri.parse(url));
    } catch (e) {
      vscode.window.showErrorMessage(t('act.openLinkFailed', { err: (e as Error).message }));
    }
  }

  /** 文档指南: 弹出英文 / 中文官网选项并跳转。 */
  async docGuide(): Promise<void> {
    const zh = t('act.docGuideZh');
    const choice = await pick([t('act.docGuideEn'), zh], t('act.docGuidePlaceholder'));
    if (!choice) { return; }
    await this.openUrl(choice === zh
      ? 'https://www.quectel.com.cn/quectel-pi/documentation-center'
      : 'https://www.quectel.com/docs/quectel-pi/');
  }

  /** 示例工程: 打开 Quectel Pi 的 GitHub 组织页。 */
  async openSamples(): Promise<void> {
    await this.openUrl('https://github.com/Quectel-Pi');
  }

  // ---------- 打开已有工程 ----------
  async openApplication(): Promise<void> {
    const last = this.context.workspaceState.get<string>('qpiSdk.lastProjectDir') ?? '';
    const dir = await vscode.window.showInputBox({
      prompt: t('openApp.pathPrompt'),
      value: last,
      ignoreFocusOut: true,
    });
    if (!dir) { return; }

    // 去除粘贴时可能带入的前后空格/换行, 否则 stat 会误报“路径不存在”
    const trimmed = dir.trim();
    if (!trimmed) { return; }

    // 展开 ~ (Home), 兼容 Windows 反斜杠与 WSL UNC 路径
    const home = process.env.HOME || process.env.USERPROFILE || '';
    const resolvedDir = trimmed.replace(/^~(?=[\\/]|$)/, home);

    // 用与 vscode.openFolder 完全相同的 URI 机制校验, 避免 Node fs 与 VS Code 行为不一致
    const uri = vscode.Uri.file(resolvedDir);
    try {
      const st = await vscode.workspace.fs.stat(uri);
      if ((st.type & vscode.FileType.Directory) === 0) {
        vscode.window.showWarningMessage(t('openApp.invalidPath', { path: resolvedDir }));
        return;
      }
    } catch (e) {
      const code = (e as { code?: string })?.code;
      const isWsl = /^\\\\wsl/i.test(resolvedDir);
      const hint = isWsl ? ' ' + t('openApp.wslHint') : '';
      vscode.window.showWarningMessage(
        t('openApp.invalidPath', { path: resolvedDir }) + (code ? ` (${code})` : '') + hint,
      );
      return;
    }
    await this.context.workspaceState.update('qpiSdk.lastProjectDir', trimmed);

    const choice = await vscode.window.showQuickPick(
      [
        { label: '$(add) '          + t('openApp.addWs'),  description: t('openApp.addWs.desc'),  id: 'add' as const },
        { label: '$(empty-window) ' + t('openApp.newWin'), description: t('openApp.newWin.desc'), id: 'new' as const },
      ],
      { placeHolder: t('openApp.whereOpen') },
    );
    if (!choice) { return; }
    switch (choice.id) {
      case 'new':
        await vscode.commands.executeCommand('vscode.openFolder', uri, { forceNewWindow: true });
        break;
      case 'add':
      default:
        await vscode.workspace.updateWorkspaceFolders(vscode.workspace.workspaceFolders?.length ?? 0, 0, { uri });
        break;
    }
  }

  /** 生成一个"下发任务到终端"的无参命令 */
  private task(title: string, launcher: (api: ApiClient) => Promise<{ task_id: string }>) {
    return async (): Promise<void> => {
      if (!(await this.ensureConnected())) { return; }
      await this.tasks.run(title, launcher);
    };
  }
}

/* ---------------- sudo 密码采集 (打包 rootfs / build all 需要 root) ----------------

   插件经管道执行命令、没有 tty, sudo 无法弹出密码提示, 会以
   "sudo: a terminal is required to read the password" 失败。所以由插件采集密码,
   经环境变量 QPI_SUDO_PASS 交给 SDK 的打包脚本 (SDK 侧用 sudo -A + SUDO_ASKPASS 消费)。

   放在这里是为了让两个入口共用: ① 侧边栏/命令 -> Actions.build
   ② 面板 Webview 按钮 -> webview/panel.ts 的 start-task (它按方法名反射直调 api,
   拿不到"新增的第 3 个参数", 曾因此漏掉密码、把 sudo 报错刷满面板)。
   两边共用同一份缓存, 一个会话只问一次。                                              */

/** 本会话缓存的 sudo 密码: 只在扩展进程内存, 不落盘; 认证失败时清除 */
let sudoCache = '';
/** "密码错"是否已提示过: 面板流式输出会连续命中多行, 避免刷屏 */
let sudoReported = false;

/** 本会话是否已探测/处理过本机 adb 环境: 避免每次设备操作都重复探测与重复安装。
 *  连接/重连/重启后端时重置 (宿主或设备位置可能已变化)。 */
let adbEnvDone = false;
/** 重置 adb 环境标记 (连接/重连/重启后端后调用, 让下次设备操作重新探测) */
export function resetAdbEnvDone(): void { adbEnvDone = false; }
/** adb 安装任务最长等待时长: 超时不再等 (任务被手动关闭时 onDone 不会回调) */
const ADB_INSTALL_WAIT_MS = 300000;

/** 密码错: 清缓存并提示。不清的话重跑会一直用错密码, 用户看不出原因。 */
export function reportSudoError(): void {
  sudoCache = '';
  if (sudoReported) { return; }
  sudoReported = true;
  void vscode.window.showErrorMessage(t('msg.sudoWrong'));
}

/** 探测并按需索取 sudo 密码。
 *  返回 ''        → 该模式不需要密码 (免密 sudo 或不需要 root 的模式)
 *  返回 undefined → 用户取消, 调用方应中止任务 */
export async function ensureSudoPassword(api: ApiClient, mode: string): Promise<string | undefined> {
  // 每发起一次编译就重置"已提示密码错"标记: 否则第一次输错提示过之后,
  // 后面再输错(或换了密码仍失败)就一声不吭, 用户不知道发生了什么。
  sudoReported = false;
  let needed = false;
  let remote = false;
  let reason = '';
  let dockerMissing = false;
  let hostOs = '';
  try {
    const r = await api.sudoCheck(mode);
    needed = r.needed;
    remote = r.remote;
    reason = r.reason;
    dockerMissing = r.docker_missing === true;
    hostOs = r.host_os ?? '';
  } catch {
    return ''; // 探测失败不阻塞: 交给后端脚本自己报错
  }
  // Docker 未安装: 弹密码没有意义, 直接提示安装方式并中止 (macOS 用 Homebrew + colima 自动装)
  if (dockerMissing) {
    output.log(`${t('sudo.logNeeded')} ${reason}`);
    void vscode.window.showWarningMessage(
      hostOs === 'darwin' ? t('docker.missingMac') : t('docker.missingHint'));
    return undefined;
  }
  if (!needed) { return ''; }
  // 后端 reason 是固定中文 (后端没有 UI 语言概念), 只写日志做诊断;
  // 用户可见文案一律走 i18n, 否则英文界面会中英混排。
  output.log(`${t('sudo.logNeeded')} ${reason}`);
  if (sudoCache) { return sudoCache; }
  // SSH 模式密码经加密通道 stdin 注入远端 (不进编译机进程列表), 可安全收集;
  // 提示用户输入的是编译机账号的 sudo 密码。
  const pwd = await vscode.window.showInputBox({
    prompt: remote ? t('prompt.sudoPasswordSsh') : t('prompt.sudoPassword'),
    password: true,
    ignoreFocusOut: true,
    placeHolder: 'sudo',
  });
  if (!pwd) { return undefined; }
  sudoCache = pwd;
  return pwd;
}

/** 供 panel.ts 一处 import (转出 taskterminal 里的判定函数) */
export { isSudoAuthError } from './taskterminal';
