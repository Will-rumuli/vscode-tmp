import * as vscode from 'vscode';
import { BackendManager } from './backend';
import { ApiClient } from './api';
import { SidebarProvider } from './sidebar';
import { StatusBar } from './statusbar';
import { TaskTerminalManager } from './taskterminal';
import { registerWelcomeView } from './welcome';
import { t, currentLanguage, setLanguage, Lang } from './i18n';
import { openAiAssistant, openOaiConfig } from './ai';
import { configureDownloadCommand } from './download';
import { ActionHandlers, resetAdbEnvDone } from './actions';
import { BasePanel } from './webview/base';
import * as output from './output';

let backend: BackendManager | null = null;

export function activate(context: vscode.ExtensionContext): void {
  // 语言: 用户上次手动切换的选择优先, 未选择时跟随 IDE 语言。
  // 必须在构造任何 UI 之前设置, 否则首屏文案仍是旧语言。
  const savedLang = context.globalState.get<Lang>('qpiSdk.language');
  if (savedLang) { setLanguage(savedLang); }

  output.log(t('backend.logActivated', { ver: String(context.extension.packageJSON?.version ?? '') }));
  backend = new BackendManager(context);
  const api = new ApiClient(() => backend!.getHandle());

  const tasks = new TaskTerminalManager(() => api);
  const actions = new ActionHandlers(context, () => api, backend, tasks);
  const statusBar = new StatusBar();
  context.subscriptions.push(statusBar);

  // ---- 侧边栏 WebviewView (accordion + tiles) ----
  const sidebar = new SidebarProvider(context, () => api);
  context.subscriptions.push(vscode.window.registerWebviewViewProvider(SidebarProvider.viewId, sidebar));

  // ---- "管理资源" 原生 TreeView ----
  const welcome = registerWelcomeView(context, {
    docGuide: () => actions.docGuide(),
    manageSdks: () => actions.manageSdks(),
    openApplication: () => actions.openApplication(),
    browseSamples: () => actions.openSamples(),
    openTerminal: () => {
      const term = vscode.window.createTerminal({ name: t('msg.termLabel') });
      term.show();
    },
  });

  // ---- 命令注册 ----
  const reg = (id: string, fn: (...args: any[]) => unknown) =>
    context.subscriptions.push(vscode.commands.registerCommand(id, (...args) => fn(...args)));

  reg('qpiSdk.restartBackend', async () => {
    backend!.stop();
    try { await backend!.start(); } catch (e) {
      vscode.window.showErrorMessage(t('msg.backendStartFailed', { err: (e as Error).message }), t('msg.retry'));
      return;
    }
    resetAdbEnvDone(); // 后端重启后 adb 缓存清空, 前端标记同步重置
    await refreshAll();
    vscode.window.showInformationMessage(t('msg.backendRestarted'));
  });

  // ---- 语言切换: 中英互切, 选择持久化到 globalState (跨工作区) ----
  reg('qpiSdk.toggleLanguage', async () => {
    const next: Lang = currentLanguage() === 'zh' ? 'en' : 'zh';
    setLanguage(next);
    await context.globalState.update('qpiSdk.language', next);
    // 已渲染的 UI 需重建才能拿到新文案: webview 的字典与静态文案都是生成 HTML 时注入的
    sidebar.retranslate();
    statusBar.retranslate();
    BasePanel.reloadAll();
    welcome.retranslate();
    await refreshAll();
  });

  // ---- 连接 / 设备 ----
  reg('qpiSdk.connect', () => actions.connect().then(refreshAll));
  reg('qpiSdk.disconnect', () => actions.disconnect().then(refreshAll));
  reg('qpiSdk.refreshDevices', () => actions.refreshDevices());

  // ---- 编译 ----
  reg('qpiSdk.buildApp', actions.build('app', t('task.buildApp')));
  reg('qpiSdk.buildAll', actions.build('all', t('task.buildAll')));
  reg('qpiSdk.buildBoot', actions.build('boot', t('task.buildBoot')));
  // 编译内核: Yocto SDK (buildkernel) 专属按钮, 侧边栏按平台显隐
  reg('qpiSdk.buildKernel', actions.build('kernel', t('task.buildKernel')));
  reg('qpiSdk.buildCheck', actions.build('check', t('task.buildCheck')));
  reg('qpiSdk.buildClean', actions.build('clean', t('task.buildClean')));
  reg('qpiSdk.setupBuildEnv', () => actions.setupBuildEnv());
  reg('qpiSdk.deviceTree', () => actions.deviceTree());

  // ---- 烧录 ----
  reg('qpiSdk.rebootLoader', () => actions.rebootLoader());
  reg('qpiSdk.flash', () => actions.flash());
  reg('qpiSdk.flashBoot', () => actions.flashBoot());
  // 烧录编译产物 (Yocto SDK 专属): 一次推送 dtb.bin + efi.bin 到设备分区
  reg('qpiSdk.deployYoctoKernel', () => actions.deployYoctoKernel());
  reg('qpiSdk.deploy', () => actions.deploy());

  // ---- 设备 / 调试 ----
  reg('qpiSdk.deviceInfo', () => actions.deviceInfo());
  reg('qpiSdk.adb', () => actions.adb());
  reg('qpiSdk.dmesg', () => actions.dmesg());
  reg('qpiSdk.journalctl', () => actions.journalctl());
  reg('qpiSdk.reboot', () => actions.reboot());
  reg('qpiSdk.at', () => actions.at());
  reg('qpiSdk.exec', () => actions.execShell());

  // ---- 音频 / 性能 ----
  reg('qpiSdk.audioCollect', () => actions.audioCollect());
  reg('qpiSdk.tinymix', () => actions.tinymix());
  reg('qpiSdk.aplay', () => actions.aplay());
  reg('qpiSdk.videoPlay', () => actions.videoPlay());
  reg('qpiSdk.screenRecord', () => actions.screenRecord());
  reg('qpiSdk.screenshot', actions.screenshot);
  reg('qpiSdk.perfMonitor', () => actions.perfMonitor());

  // ---- 文档 / 工程 / 设备树 / 环境 ----
  reg('qpiSdk.skills', () => actions.skills());
  reg('qpiSdk.createProject', () => actions.createProject());
  reg('qpiSdk.projects', () => actions.manageProjects());
  reg('qpiSdk.repo', () => actions.setupEnv());
  reg('qpiSdk.backendInfo', () => actions.backendInfo());
  reg('qpiSdk.sdkInfo', () => actions.sdkInfo());
  reg('qpiSdk.manageDocker', () => actions.manageDocker());

  // ---- AI 助手 (GitHub Copilot Chat + OpenAI 兼容 Provider) ----
  reg('qpiSdk.aiAssistant', () => openAiAssistant());
  reg('qpiSdk.oaiConfig', () => openOaiConfig());

  // ---- 统一下载: 代理 / 镜像策略 ----
  reg('qpiSdk.configureDownload', async () => {
    const changed = await configureDownloadCommand();
    if (!changed) { return; }
    // 代理/镜像在后端启动时读取, 修改后需重启后端才生效
    const pick = await vscode.window.showInformationMessage(
      t('download.cfg.needRestart'),
      t('download.cfg.restart'),
    );
    if (pick) { await vscode.commands.executeCommand('qpiSdk.restartBackend'); }
  });

  // ---- 状态轮询: 同步侧边栏 chip + 底部状态栏 ----
  async function refreshAll(): Promise<void> {
    try {
      if (!backend!.isRunning()) {
        statusBar.setConnection(false, '');
        await sidebar.refreshStatus();
        return;
      }
      const st = await api.status();
      statusBar.setConnection(st.connected, st.display);
    } catch {
      statusBar.setConnection(false, '');
    }
    await sidebar.refreshStatus();
  }

  const timer = setInterval(() => void refreshAll(), 5000);
  context.subscriptions.push({ dispose: () => clearInterval(timer) });
  void refreshAll();

  // 自动启动后端, 启动后用上次配置静默重连
  const autoStart = vscode.workspace.getConfiguration('qpiSdk').get<boolean>('backend.autoStart', true);
  if (autoStart) {
    output.log(t('backend.logAutoStart'));
    backend.start().then(async () => {
      const ok = await actions.tryReconnect();
      output.log(ok ? t('backend.logReconnected') : t('backend.logNoReconnect'));
      await refreshAll();
    }).catch((e) => {
      output.log(t('backend.logAutoStartFailed', { err: e instanceof Error ? e.message : String(e) }));
      console.error('[qpiSdk] 后端自动启动失败:', e);
    });
  } else {
    output.log(t('backend.logAutoStartOff'));
  }
}

export function deactivate(): void {
  if (backend) {
    backend.stop();
  }
  output.disposeOutput();
}
