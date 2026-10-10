/* 独立 Webview 面板基类: 统一创建/单例复用/HTML 骨架/CSP/i18n 注入/消息收发。
   新增面板只需继承 BasePanel 并提供 body/style/onMessage, 无需重复样板。 */
import * as vscode from 'vscode';
import { t, isZh, webviewDict } from '../i18n';

export interface PanelOptions {
  /** 面板唯一标识 (viewType), 如 'qpiSdkDeviceTree' */
  viewType: string;
  /** 面板标题; 传函数则在每次渲染时求值 (语言切换后可重渲染) */
  title: string | (() => string);
  extensionUri: vscode.Uri;
  /** 相对扩展根的 webview 脚本, 如 'webview/devicetree.js' */
  jsFile: string;
  /** <body> 内部 HTML (模板字符串可先渲染 ${t(...)}); 传函数则每次渲染时求值 */
  body: string | (() => string);
  /** 追加到公共样式之后的 <style> 内容 (面板特有样式/覆盖) */
  style?: string;
  /** script-src 是否允许 unsafe-inline (内联 i18n 字典需要), 默认 true */
  unsafeInline?: boolean;
  onMessage: (msg: any) => void;
}

/** 取值: 支持字面量或惰性求值函数 (后者用于语言切换后重渲染) */
function resolve(v: string | (() => string)): string {
  return typeof v === 'function' ? v() : v;
}

/** HTML 属性值转义 (用于 placeholder 等) */
export const escAttr = (s: string): string =>
  s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

/** 所有面板共享的公共样式: 主题变量 + 通用组件 (字段/输入框/下拉/按钮/分组/toast) */
export abstract class BasePanel {
  readonly panel: vscode.WebviewPanel;
  private static registry = new Map<string, BasePanel>();
  private readonly opts: PanelOptions;
  private readonly jsUri: vscode.Uri;
  private readonly cssUri: vscode.Uri;

  protected constructor(opts: PanelOptions) {
    this.opts = opts;
    this.panel = vscode.window.createWebviewPanel(opts.viewType, resolve(opts.title), vscode.ViewColumn.One, {
      enableScripts: true,
      retainContextWhenHidden: true,
      localResourceRoots: [
        vscode.Uri.joinPath(opts.extensionUri, 'webview'),
        vscode.Uri.joinPath(opts.extensionUri, 'src', 'webview'),
      ],
    });
    this.jsUri = this.panel.webview.asWebviewUri(vscode.Uri.joinPath(opts.extensionUri, opts.jsFile));
    this.cssUri = this.panel.webview.asWebviewUri(
      vscode.Uri.joinPath(opts.extensionUri, 'src', 'webview', 'common.css'));
    this.panel.webview.html = this.html(opts, this.jsUri, this.cssUri);
    this.panel.webview.onDidReceiveMessage((m) => opts.onMessage(m));
  }

  /** 单例复用: 已存在则聚焦, 否则用 factory 创建并登记 (关闭自动注销) */
  static reuse<T extends BasePanel>(key: string, factory: () => T): T {
    const existing = BasePanel.registry.get(key) as T | undefined;
    if (existing) {
      existing.panel.reveal(vscode.ViewColumn.One);
      return existing;
    }
    const p = factory();
    BasePanel.registry.set(key, p);
    p.panel.onDidDispose(() => {
      if (BasePanel.registry.get(key) === p) BasePanel.registry.delete(key);
    });
    return p;
  }

  post(type: string, payload?: unknown): void {
    void this.panel.webview.postMessage({ type, payload });
  }

  /** 语言切换时重渲染所有已打开面板 (单例 key 不变; 面板内临时状态会重置) */
  static reloadAll(): void {
    for (const p of BasePanel.registry.values()) {
      p.panel.title = resolve(p.opts.title);
      p.panel.webview.html = p.html(p.opts, p.jsUri, p.cssUri);
    }
  }

  /** 任意抛错转可读文本, 避免显示 "undefined" */
  errText(e: unknown): string {
    if (e instanceof Error) return e.message || String(e);
    return e === undefined ? t('act.unknownError') : String(e);
  }

  dispose(): void {
    this.panel.dispose();
  }

  private html(opts: PanelOptions, jsUri: vscode.Uri, cssUri: vscode.Uri): string {
    const csp = this.panel.webview.cspSource;
    const allowInline = opts.unsafeInline !== false;
    const i18n = allowInline
      ? `<script>window.QPI_I18N = ${JSON.stringify(webviewDict()).replace(/</g, '\\u003c')};</script>`
      : '';
    return `<!DOCTYPE html>
<html lang="${isZh() ? 'zh-CN' : 'en'}">
<head>
<meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src ${csp} 'unsafe-inline'; script-src ${csp}${allowInline ? " 'unsafe-inline'" : ''};">
<link rel="stylesheet" href="${cssUri}">
<style>${opts.style ?? ''}</style>
</head>
<body>
${resolve(opts.body)}
${i18n}
<script src="${jsUri}"></script>
</body>
</html>`;
  }
}
