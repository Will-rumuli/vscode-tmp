/* Webview 面板管理: 创建面板、加载 HTML、与 webview 双向通信 */
import * as vscode from 'vscode';
import * as path from 'path';
import { BackendManager } from '../backend';
import { ApiClient } from '../api';
import { t, isZh, webviewDict } from '../i18n';
import { ensureSudoPassword, isSudoAuthError, reportSudoError } from '../actions';
import { getYoctoThreads } from '../yoctoThreads';
import { yoctoBuildPayload } from '../yoctoBuildParams';

interface PanelMsg {
  type: string;
  [k: string]: unknown;
}

export class PanelManager {
  private panel: vscode.WebviewPanel | null = null;
  private disposables: vscode.Disposable[] = [];

  constructor(
    private context: vscode.ExtensionContext,
    private getApi: () => ApiClient,
    private getBackend: () => BackendManager,
  ) {}

  show(): void {
    if (this.panel) {
      this.panel.reveal(vscode.ViewColumn.One);
      return;
    }
    this.panel = vscode.window.createWebviewPanel(
      'qpiSdkPanel',
      t('panel.title'),
      vscode.ViewColumn.One,
      {
        enableScripts: true,
        retainContextWhenHidden: true,
        localResourceRoots: [vscode.Uri.file(path.join(this.context.extensionPath, 'src', 'webview'))],
      },
    );
    this.panel.webview.html = this.getHtml();
    this.panel.webview.onDidReceiveMessage((msg: PanelMsg) => this.onMessage(msg), undefined, this.disposables);
    this.panel.onDidDispose(() => {
      this.panel = null;
    }, undefined, this.disposables);
  }

  refreshStatus(): void {
    this.panel?.webview.postMessage({ type: 'backend-restarted' });
  }

  /** 打开面板后切换到指定标签页 (由侧边栏卡片触发, 复用复杂页面) */
  switchTab(tab: string): void {
    if (!this.panel) { return; }
    setTimeout(() => {
      this.panel?.webview.postMessage({ type: 'switch-tab', tab });
    }, 400);
  }

  private async onMessage(msg: PanelMsg): Promise<void> {
    const api = this.getApi();
    const backend = this.getBackend();
    try {
      switch (msg.type) {
        case 'backend-info': {
          const h = backend.getHandle();
          this.post({ type: 'backend-info', running: backend.isRunning(), port: h?.port, key: h?.key });
          break;
        }
        case 'status': {
          const st = await api.status();
          this.post({ type: 'status', status: st });
          break;
        }
        case 'connect': {
          const conn = msg.conn as Record<string, unknown>;
          const r = await api.connect(conn as never);
          this.post({ type: 'connect-result', ok: true, data: r });
          break;
        }
        case 'disconnect': {
          await api.disconnect();
          this.post({ type: 'disconnect-result', ok: true, data: {} });
          break;
        }
        case 'probe': {
          const r = await api.probe();
          this.post({ type: 'probe', data: r });
          break;
        }
        case 'devices': {
          const r = await api.devices();
          this.post({ type: 'devices', data: r });
          break;
        }
        case 'templates': {
          const r = await api.templates();
          this.post({ type: 'templates', data: r });
          break;
        }
        case 'projects': {
          const r = await api.projects();
          this.post({ type: 'projects', data: r });
          break;
        }
        case 'examples': {
          const r = await api.examples();
          this.post({ type: 'examples', data: r });
          break;
        }
        case 'flash-tools': {
          const r = await api.flashTools();
          this.post({ type: 'flash-tools', data: r });
          break;
        }
        case 'device-tree': {
          const r = await api.deviceTree();
          this.post({ type: 'device-tree', data: r });
          break;
        }
        case 'device-tree-status': {
          const r = await api.setDeviceTreeStatus(String(msg.node ?? ''), Boolean(msg.enabled), String(msg.source ?? ''));
          this.post({ type: 'device-tree-status-result', result: r, node: msg.node, source: msg.source });
          break;
        }
        case 'device-tree-properties': {
          const r = await api.setDeviceTreeProperties(
            String(msg.node ?? ''), String(msg.source ?? ''), String(msg.properties ?? ''));
          this.post({ type: 'device-tree-properties-result', result: r, node: msg.node, source: msg.source });
          break;
        }
        case 'device-tree-source': {
          const r = await api.deviceTreeSource(String(msg.node ?? ''), String(msg.label ?? ''));
          this.post({ type: 'device-tree-source-result', result: r });
          break;
        }
        case 'ports': {
          const r = await api.ports();
          this.post({ type: 'ports', data: r });
          break;
        }
        case 'open-device-info': {
          // 设备信息已改为独立结构化面板: 触发宿主命令打开 (仅需后端 + adb 设备)
          await vscode.commands.executeCommand('qpiSdk.deviceInfo');
          break;
        }
        case 'open-perf-monitor': {
          // 性能监控为独立实时面板: 触发宿主命令打开 (仅需后端 + adb 设备)
          await vscode.commands.executeCommand('qpiSdk.perfMonitor');
          break;
        }
        case 'skills': {
          const r = await api.skills();
          this.post({ type: 'skills', data: r });
          break;
        }
        case 'skill-content': {
          const name = msg.name as string;
          const r = await api.skill(name);
          this.post({ type: 'skill-content', name, content: r.content });
          break;
        }
        case 'project-create': {
          const p = msg as unknown as { name: string; template: string; variables: Record<string, unknown> };
          const r = await api.projectCreate(p.name, p.template, p.variables);
          this.post({ type: 'project-create-result', result: r });
          break;
        }
        case 'project-delete': {
          const name = msg.name as string;
          const r = await api.projectDelete(name);
          this.post({ type: 'project-delete-result', result: r });
          break;
        }
        case 'repo-status': {
          const repos = (msg.repos as Record<string, string>) ?? {};
          const r = await api.repoStatus(repos);
          this.post({ type: 'repo-status', data: r });
          break;
        }
        case 'start-task': {
          // 通用任务启动: 由 webview 传入 api method 名 + 参数
          const method = msg.method as string;
          let args = (msg.args as unknown[]) ?? [];
          // build 的 all/rootfs 会打包 rootfs, 需要 root 权限。与命令入口保持一致:
          // 先探测、必要时弹密码框, 再把密码作为第 3 个参数交给 api.build。
          // (此前这里按方法名直接直调, 拿不到密码 -> sudo 认证失败, 报错刷满面板)
          if (method === 'build') {
            const mode = (args[0] as string) || 'all';
            const pwd = await ensureSudoPassword(api, mode);
            if (pwd === undefined) {
              this.post({ type: 'task-cancelled', method, args });
              return;
            }
            // 第 4/5 参: Yocto 并行度 + buildconfig 参数 (非 Yocto SDK 由后端忽略)
            args = [mode, (args[1] as string) ?? '', pwd,
              getYoctoThreads(this.context), yoctoBuildPayload(this.context)];
          }
          // 烧录: 远端设备模式下烧录脚本内部 sudo 需要密码 (免密时返回 '' 不打扰)
          if (method === 'flash') {
            const pwd = await ensureSudoPassword(api, 'flash');
            if (pwd === undefined) {
              this.post({ type: 'task-cancelled', method, args });
              return;
            }
            args = [(args[0] as string) ?? 'upgrade_tool',
                    (args[1] as string) ?? 'build/result/update.img', pwd || ''];
          }
          const fn = (api as unknown as Record<string, (...a: unknown[]) => Promise<{ task_id: string }>>)[method];
          if (typeof fn !== 'function') {
            this.post({ type: 'task-error', method, args, error: t('panel.unknownApi', { method }) });
            return;
          }
          const r = await fn(...args);
          this.post({ type: 'task-started', taskId: r.task_id, method, args });
          break;
        }
        case 'kill-task': {
          await api.killTask(msg.taskId as string);
          break;
        }
        case 'stream-task': {
          // 后端流式输出转发给 webview
          const taskId = msg.taskId as string;
          const method = msg.method as string;
          void api.stream(taskId, (ev) => {
            // 密码错/无 tty: 清掉缓存的密码并提示, 否则用户重跑会一直用错密码
            if ((ev.type === 'out' || ev.type === 'err') && isSudoAuthError(ev.line)) {
              reportSudoError();
            }
            this.post({ type: 'stream-event', taskId, method, event: ev });
          }, (err) => {
            this.post({ type: 'stream-error', taskId, method, error: err });
          }).catch((e) => {
            this.post({ type: 'stream-error', taskId, method, error: String(e) });
          });
          break;
        }
        case 'browse-sdk': {
          const picked = await vscode.window.showOpenDialog({
            canSelectFolders: true, canSelectFiles: false,
            openLabel: t('connect.browse'),
            title: t('panel.browseSdkTitle'),
          });
          if (picked && picked.length > 0) {
            this.post({ type: 'sdk-path-picked', path: picked[0].fsPath });
          }
          break;
        }
        case 'save-connection': {
          await this.context.workspaceState.update('qpiSdk.connection', msg.conn);
          break;
        }
        case 'save-repos-persist': {
          await this.context.workspaceState.update('qpiSdk.repos', msg.repos);
          break;
        }
        case 'load-repos': {
          const repos = this.context.workspaceState.get('qpiSdk.repos');
          this.post({ type: 'load-repos', repos: repos ?? null });
          break;
        }
        case 'load-connection': {
          const conn = this.context.workspaceState.get('qpiSdk.connection');
          this.post({ type: 'load-connection', conn: conn ?? null });
          break;
        }
        case 'open-folder': {
          const rel = String(msg.rel ?? '');
          const sdkRoot = (msg.sdkRoot as string) ?? '';
          const conn = (msg.conn as Record<string, unknown>) ?? {};
          const isSsh = conn.mode === 'ssh';
          // Windows 扩展宿主下 path.join 会把远程 /home/... 拼成反斜杠，
          // 导致 vscode-remote URI 的 authority + path 组合无效。
          const abs = isSsh
            ? (rel.startsWith('/') ? rel : path.posix.join(sdkRoot, rel))
            : (path.isAbsolute(rel) ? rel : path.join(sdkRoot, rel));
          try {
            if (isSsh) {
              const host = String(conn.host ?? '');
              if (!host) { throw new Error(t('panel.sshHostMissing')); }
              // 用户已通过 SMB 共享暴露远程 SDK。优先在 Windows 资源管理器中
              // 打开共享目录，避免 Remote-SSH 默认以 Windows 域用户重新认证。
              const unc = `\\\\${host}\\share${abs.replace(/\//g, '\\')}`;
              await vscode.commands.executeCommand('revealInExplorer', vscode.Uri.file(unc));
            } else {
              await vscode.commands.executeCommand('revealInExplorer', vscode.Uri.file(abs));
            }
            this.post({ type: 'open-folder-result', ok: true });
          } catch (e) {
            this.post({ type: 'open-folder-result', ok: false, error: (e as Error).message });
          }
          break;
        }
        default:
          break;
      }
    } catch (e) {
      this.post({ type: 'error', method: msg.method, args: msg.args, error: (e as Error).message });
    }
  }

  private post(msg: unknown): void {
    this.panel?.webview.postMessage(msg);
  }

  private getHtml(): string {
    const webviewRoot = vscode.Uri.file(path.join(this.context.extensionPath, 'src', 'webview'));
    const htmlPath = path.join(this.context.extensionPath, 'src', 'webview', 'panel.html');
    const fs = require('fs') as typeof import('fs');
    let html = fs.readFileSync(htmlPath, 'utf-8');

    const cssUri = this.panel!.webview.asWebviewUri(vscode.Uri.joinPath(webviewRoot, 'common.css'));
    const jsUri = this.panel!.webview.asWebviewUri(vscode.Uri.joinPath(webviewRoot, 'panel.js'));

    // 注入插件版本号 (顶部标题显示具体版本)
    let version = '';
    try {
      const pkg = JSON.parse(fs.readFileSync(path.join(this.context.extensionPath, 'package.json'), 'utf-8'));
      version = pkg.version || '';
    } catch { /* 忽略 */ }

    // 注入当前语言字典, 供 webview 的 panel.js 本地化静态/动态文案
    const i18nJson = JSON.stringify(webviewDict()).replace(/</g, '\\u003c');
    html = html.replace('{{VERSION}}', version)
      .replace('{{CSS_URI}}', cssUri.toString())
      .replace('{{JS_URI}}', jsUri.toString())
      .replace('{{LANG}}', isZh() ? 'zh-cn' : 'en')
      .replace('{{I18N_JSON}}', i18nJson);
    return html;
  }
}
