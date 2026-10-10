import * as vscode from 'vscode';
import { ApiClient } from './api';
import { t, isZh } from './i18n';

interface Tile {
  cmd: string; icon: string; labelKey: string; tipKey?: string; color?: 'danger';
  /** 仅 Yocto SDK (sdk_type=yocto) 显示; 默认隐藏 */
  yoctoOnly?: boolean;
  /** Yocto SDK 时隐藏 (如 H1 的"烧录 Boot"仅 Simple SDK 可用) */
  yoctoHide?: boolean;
}
interface Region { id: string; icon: string; titleKey: string; tiles: Tile[]; }

function buildRegions(): Region[] {
  return [
    {
      id: 'connect', icon: 'plug', titleKey: 'region.connect',
      tiles: [
        { cmd: 'qpiSdk.connect', icon: 'plug', labelKey: 'tile.connect', tipKey: 'tile.connect.tip' },
        { cmd: 'qpiSdk.disconnect', icon: 'debug-disconnect', labelKey: 'tile.disconnect', tipKey: 'tile.disconnect.tip' },
        { cmd: 'qpiSdk.refreshDevices', icon: 'refresh', labelKey: 'tile.refreshDev' },
        { cmd: 'qpiSdk.sdkInfo', icon: 'git-commit', labelKey: 'tile.sdkInfo', tipKey: 'tile.sdkInfo.tip' },
        { cmd: 'qpiSdk.backendInfo', icon: 'info', labelKey: 'tile.backendInfo' },
        { cmd: 'qpiSdk.repo', icon: 'repo', labelKey: 'tile.repo', tipKey: 'tile.repo.tip' },
      ],
    },
    {
      id: 'project', icon: 'folder', titleKey: 'region.project',
      tiles: [
        { cmd: 'qpiSdk.createProject', icon: 'add', labelKey: 'tile.createProject' },
        { cmd: 'qpiSdk.projects', icon: 'folder-opened', labelKey: 'tile.projects', tipKey: 'tile.projects.tip' },
        { cmd: 'qpiSdk.skills', icon: 'book', labelKey: 'tile.skills' },
      ],
    },
    {
      id: 'build', icon: 'tools', titleKey: 'region.build',
      tiles: [
        { cmd: 'qpiSdk.setupBuildEnv', icon: 'wrench', labelKey: 'tile.setupBuildEnv', tipKey: 'tile.setupBuildEnv.tip' },
        { cmd: 'qpiSdk.buildAll', icon: 'package', labelKey: 'tile.buildAll' },
        // 编译 Boot: Simple SDK 保留 (buildboot); Yocto SDK 隐藏 (已并入「编译内核」)
        { cmd: 'qpiSdk.buildBoot', icon: 'file-binary', labelKey: 'tile.buildBoot', yoctoHide: true },
        // 编译内核: 仅 Yocto SDK 显示; 一次执行 builddtb + buildkernel (Boot + 内核)
        { cmd: 'qpiSdk.buildKernel', icon: 'circuit-board', labelKey: 'tile.buildKernel', yoctoOnly: true },
        // 设备树卡片暂隐藏;恢复卡片: 取消下行注释
        // { cmd: 'qpiSdk.deviceTree', icon: 'code', labelKey: 'tile.deviceTree' },
        { cmd: 'qpiSdk.buildClean', icon: 'trash', labelKey: 'tile.buildClean', color: 'danger' },
      ],
    },
    {
      id: 'flash', icon: 'flame', titleKey: 'region.flash',
      tiles: [
        { cmd: 'qpiSdk.flash', icon: 'zap', labelKey: 'tile.flash' },
        // H1 全盘烧录 SDK: 不支持单独烧 boot; Simple SDK 保留, Yocto SDK 隐藏
        { cmd: 'qpiSdk.flashBoot', icon: 'rocket', labelKey: 'tile.flashBoot', yoctoHide: true },
        // 烧录内核 (dtb.bin + efi.bin 一次部署到设备分区): 仅 Yocto SDK 显示
        { cmd: 'qpiSdk.deployYoctoKernel', icon: 'desktop-download', labelKey: 'tile.deployKernel', tipKey: 'tile.deployKernel.tip', yoctoOnly: true },
        { cmd: 'qpiSdk.rebootLoader', icon: 'symbol-event', labelKey: 'tile.rebootLoader' },
        { cmd: 'qpiSdk.deploy', icon: 'cloud-upload', labelKey: 'tile.deploy' },
        { cmd: 'qpiSdk.deviceInfo', icon: 'device-mobile', labelKey: 'tile.deviceInfo' },
        { cmd: 'qpiSdk.reboot', icon: 'debug-restart', labelKey: 'tile.reboot' },
      ],
    },
    {
      id: 'log', icon: 'file-text', titleKey: 'region.log',
      tiles: [
        { cmd: 'qpiSdk.adb', icon: 'terminal', labelKey: 'tile.adb' },
        { cmd: 'qpiSdk.dmesg', icon: 'list-tree', labelKey: 'tile.dmesg' },
        { cmd: 'qpiSdk.journalctl', icon: 'list-flat', labelKey: 'tile.journalctl' },
        { cmd: 'qpiSdk.at', icon: 'send', labelKey: 'tile.at' },
        { cmd: 'qpiSdk.exec', icon: 'terminal-bash', labelKey: 'tile.exec' },
        { cmd: 'qpiSdk.audioCollect', icon: 'mic', labelKey: 'tile.audioCollect' },
      ],
    },
    {
      id: 'audio', icon: 'graph-line', titleKey: 'region.audio',
      tiles: [
        { cmd: 'qpiSdk.aplay', icon: 'unmute', labelKey: 'tile.aplay' },
        { cmd: 'qpiSdk.videoPlay', icon: 'play-circle', labelKey: 'tile.videoPlay' },
        { cmd: 'qpiSdk.screenRecord', icon: 'device-camera-video', labelKey: 'tile.screenRecord' },
        { cmd: 'qpiSdk.screenshot', icon: 'device-camera', labelKey: 'tile.screenshot' },
        { cmd: 'qpiSdk.perfMonitor', icon: 'graph-line', labelKey: 'tile.perfMonitor', tipKey: 'tile.perfMonitor.tip' },
      ]},
    { id: 'ai', icon: 'copilot', titleKey: 'region.ai', tiles: [
        { cmd: 'qpiSdk.aiAssistant', icon: 'copilot', labelKey: 'tile.aiAssistant', tipKey: 'tile.aiAssistant.tip' },
        { cmd: 'qpiSdk.oaiConfig', icon: 'key', labelKey: 'tile.oaiConfig', tipKey: 'tile.oaiConfig.tip' },
      ]},
    ];
}
const REGIONS = buildRegions();

export class SidebarProvider implements vscode.WebviewViewProvider {
  static readonly viewId = 'qpiSdk.cards';
  private view: vscode.WebviewView | null = null;
  private pollTimer: ReturnType<typeof setInterval> | null = null;

  constructor(
    private readonly context: vscode.ExtensionContext,
    private readonly getApi: () => ApiClient,
  ) {}

  resolveWebviewView(view: vscode.WebviewView): void {
    this.view = view;
    // 视图标题来自 package.json 的 %view.cards.name% (加载时按 IDE 语言解析), 运行时切语言必须自己覆盖
    view.title = t('view.cards');
    view.webview.options = { enableScripts: true, localResourceRoots: [
      vscode.Uri.joinPath(this.context.extensionUri, 'src', 'webview'),
    ] };
    view.webview.html = this.getHtml();
    view.webview.onDidReceiveMessage(
      (msg: { type: string; cmd?: string }) => {
        if (msg.type === 'tile' && msg.cmd) {
          void vscode.commands.executeCommand(msg.cmd);
        } else if (msg.type === 'connect') {
          void vscode.commands.executeCommand('qpiSdk.connect');
        } else if (msg.type === 'ready') {
          void this.refreshStatus();
        }
      },
      undefined,
      this.context.subscriptions,
    );
    view.onDidChangeVisibility(() => {
      if (view.visible) {
        void this.refreshStatus();
        this.startPolling();
      } else {
        this.stopPolling();
      }
    });
    this.startPolling();
  }

  private startPolling(): void {
    this.stopPolling();
    this.pollTimer = setInterval(() => void this.refreshStatus(), 5000);
  }

  private stopPolling(): void {
    if (this.pollTimer) {
      clearInterval(this.pollTimer);
      this.pollTimer = null;
    }
  }

  /** 语言切换后重渲染侧边栏 (HTML/内联文案/字典都随语言变化) */
  retranslate(): void {
    if (this.view) {
      this.view.title = t('view.cards');
      this.view.webview.html = this.getHtml();
    }
  }

  async refreshStatus(): Promise<void> {
    if (!this.view) { return; }
    try {
      const st = await this.getApi().status();
      await this.view.webview.postMessage({
        type: 'status',
        connected: st.connected,
        display: st.display,
        platform: st.platform ?? null,
      });
    } catch {
      await this.view.webview.postMessage({ type: 'status', connected: false, display: t('msg.backendDown') });
    }
  }

  private getHtml(): string {
    const codiconCdn = 'https://cdn.jsdelivr.net/npm/@vscode/codicons@0.0.36/dist/codicon.css';
    const regionsHtml = REGIONS.map((r, idx) => {
      const tiles = r.tiles.map((tl) => {
        // 平台专属按钮: yoctoOnly 默认隐藏 (连上 Yocto SDK 后显示);
        // yoctoHide 默认显示 (连上 Yocto SDK 后隐藏)
        const yoStyle = tl.yoctoOnly ? ' style="display:none"' : '';
        const yoAttr = tl.yoctoOnly ? ' data-yocto-only="1"' : tl.yoctoHide ? ' data-yocto-hide="1"' : '';
        return `
        <div class="tile${tl.color ? ` ${tl.color}` : ''}" data-cmd="${tl.cmd}" data-tile="${tl.cmd}" title="${tl.tipKey ? t(tl.tipKey) : t(tl.labelKey)}"${yoAttr}${yoStyle}>
          <div class="ic codicon codicon-${tl.icon}"></div>
          <div class="lb">${t(tl.labelKey)}</div>
        </div>`;
      }).join('');
      return `
<div class="region${idx === 0 ? ' open' : ''}" data-region="${r.id}">
  <div class="region-h">
    <span class="ic codicon codicon-${r.icon}"></span>
    <span class="nm">${t(r.titleKey)}</span>
    <span class="cnt">${r.tiles.length}</span>
    <span class="arrow codicon codicon-chevron-down"></span>
  </div>
  <div class="region-b"><div class="region-b-inner"><div class="region-tiles">${tiles}</div></div></div>
</div>`;
    }).join('');

    const connectedText = t('sidebar.connected');
    const disconnectedText = t('sidebar.disconnected');
    const subText = t('sidebar.subtitle');
    const zh = isZh();
    const refreshText = t('sidebar.refresh');
    const restartText = t('sidebar.restartBackend');
    const langTip = t('sidebar.toggleLanguageTip');
    const footText = t('sidebar.foot');
    const detectingText = t('sidebar.detecting');
    const htmlLang = zh ? 'zh-CN' : 'en';
    const commonCssUri = this.view?.webview.asWebviewUri(
      vscode.Uri.joinPath(this.context.extensionUri, 'src', 'webview', 'common.css'));
    const commonJsUri = this.view?.webview.asWebviewUri(
      vscode.Uri.joinPath(this.context.extensionUri, 'src', 'webview', 'common.js'));
    const encodeText = (value: string): string => encodeURIComponent(value);

    return `<!DOCTYPE html>
<html lang="${htmlLang}">
<head>
<meta charset="UTF-8" />
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src ${this.view?.webview.cspSource} 'unsafe-inline' https://cdn.jsdelivr.net; font-src https://cdn.jsdelivr.net; script-src ${this.view?.webview.cspSource} 'unsafe-inline';">
<link rel="stylesheet" href="${codiconCdn}">
<link rel="stylesheet" href="${commonCssUri}">
</head>
<body class="sidebar-view"
  data-connected-text="${encodeText(connectedText)}"
  data-disconnected-text="${encodeText(disconnectedText)}"
  data-reboot-loader-label="${encodeText(t('tile.rebootLoader'))}"
  data-reboot-loader-tip="${encodeText(t('tile.rebootLoaderTip'))}"
  data-reboot-edl-label="${encodeText(t('tile.rebootEdl'))}"
  data-reboot-edl-tip="${encodeText(t('tile.rebootEdlTip'))}">
<h1><span class="logo codicon codicon-chip"></span> ${t('sidebar.title')}</h1>
<div class="sub">${subText}</div>

<div class="bar" id="bar">
  <span class="chip" id="stChip"><span class="dot"></span><b id="stLabel">${detectingText}</b></span>
  <button class="btn ico" id="refresh" title="${refreshText}"><span class="codicon codicon-refresh"></span></button>
  <button class="btn ico" id="restart" title="${restartText}"><span class="codicon codicon-server-process"></span></button>
  <button class="btn ico" id="lang" title="${langTip}"><span class="codicon codicon-globe"></span></button>
</div>

${regionsHtml}

<div class="foot">${footText}</div>

<script src="${commonJsUri}"></script>
</body>
</html>`;
  }
}

