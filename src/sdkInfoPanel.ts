/* SDK 信息面板: 展示已连接 SDK 目录的 git 状态 (分支 / 落后远端提交数 / 提交记录)。
   基于 BasePanel; 数据来自后端 /sdk/git (未连接 SDK 时后端返回 400)。
   刷新时默认联网 git fetch 一次, 保证"落后多少个版本"是当前值。 */
import * as vscode from 'vscode';
import { ApiClient, SdkGitInfo } from './api';
import { t } from './i18n';
import { BasePanel } from './webview/base';

/** SDK 信息面板特有样式 */
const SI_CSS = `
  :root{
    --warn:var(--vscode-charts-yellow,#e5c07b);
    --danger:var(--vscode-charts-red,#e06c75);
    --card:var(--vscode-sideBar-background,#1e1e1e);
  }
  .bar{margin-bottom:14px;}
  .btn{padding:5px 12px;}
  .btn.primary{background:var(--btn-bg);color:var(--btn-fg);border:none;font-weight:600;}
  .btn.primary:hover{background:var(--btn-hover);}
  .badge{display:inline-flex;align-items:center;gap:6px;font-size:13px;font-weight:600;padding:5px 14px;border-radius:14px;border:1px solid var(--border);margin-bottom:14px;}
  .badge.ok{color:var(--ok);border-color:var(--ok);}
  .badge.warn{color:var(--warn);border-color:var(--warn);}
  .badge.err{color:var(--danger);border-color:var(--danger);}
  .group{border:1px solid var(--border);border-radius:8px;margin-bottom:14px;overflow:hidden;background:var(--card);}
  .group-head{padding:9px 14px;font-size:13px;font-weight:600;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:8px;}
  .group-body{padding:12px 14px;}
  .kv{display:grid;grid-template-columns:150px minmax(0,1fr);gap:6px 12px;align-items:baseline;}
  .kv .k{color:var(--muted);font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
  .kv .v{font-size:13px;min-width:0;word-break:break-all;}
  .table{width:100%;border-collapse:collapse;font-size:12px;}
  .table th{text-align:left;color:var(--muted);font-weight:500;padding:5px 8px;border-bottom:1px solid var(--border);white-space:nowrap;}
  .table td{padding:5px 8px;border-bottom:1px solid var(--border);vertical-align:top;}
  .table tr:last-child td{border-bottom:none;}
  .table tr.head-row td{background:color-mix(in srgb,var(--accent) 14%,transparent);}
  .pill{font-size:11px;padding:1px 8px;border-radius:9px;border:1px solid var(--border);}
  .pill.local{color:var(--accent);border-color:var(--accent);}
  .note{font-size:12px;color:var(--warn);margin-top:10px;}
  #error{display:none;padding:12px 14px;border:1px solid var(--danger);color:var(--danger);border-radius:6px;font-size:13px;margin-bottom:14px;}
  #content{min-height:40px;}
`;

const SI_BODY = (): string => `
<div class="wrap">
  <h1>🔖 ${t('si.title')}</h1>
  <div class="sub">${t('si.subtitle')}</div>

  <div class="bar">
    <span class="grow"></span>
    <button class="btn" id="copyBtn">${t('si.copy')}</button>
    <button class="btn primary" id="refreshBtn">${t('si.refresh')}</button>
  </div>

  <div id="error"></div>
  <div id="badge"></div>
  <div id="content"><span class="hint">${t('si.loading')}</span></div>
</div>
<div id="toast"></div>
`;

export class SdkInfoPanel extends BasePanel {
  static open(extensionUri: vscode.Uri, getApi: () => ApiClient): SdkInfoPanel {
    return BasePanel.reuse('qpiSdkSdkInfo', () => new SdkInfoPanel(extensionUri, getApi));
  }

  private constructor(extensionUri: vscode.Uri, private readonly getApi: () => ApiClient) {
    super({
      viewType: 'qpiSdkSdkInfo',
      title: () => t('si.title'),
      extensionUri,
      jsFile: 'webview/sdkinfo.js',
      body: SI_BODY,
      style: SI_CSS,
      onMessage: (msg) => this.onMessage(msg),
    });
  }

  private onMessage(msg: any): void {
    // 刷新一律带 fetch: 面板上"落后多少个版本"必须是当前值, 不能用陈旧引用
    if (msg.type === 'refresh') {
      void this.load(true);
    } else if (msg.type === 'refreshLocal') {
      void this.load(false);
    }
  }

  private async load(fetch: boolean): Promise<void> {
    try {
      const data: SdkGitInfo = await this.getApi().sdkGit(fetch, 30);
      this.post('data', data);
    } catch (e) {
      this.post('error', this.errText(e));
    }
  }
}
