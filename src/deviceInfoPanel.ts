/* 设备信息面板: 分组卡片展示设备详细信息 (结构化 JSON), 支持刷新/复制。
   基于 BasePanel, 数据来自后端 /device/info (仅后端 + adb 设备即可)。 */
import * as vscode from 'vscode';
import { ApiClient, DeviceInfoData } from './api';
import { t } from './i18n';
import { BasePanel } from './webview/base';

/** 设备信息面板特有样式 */
const DI_CSS = `
  :root{
    --warn:var(--vscode-charts-yellow,#e5c07b);
    --danger:var(--vscode-charts-red,#e06c75);
    --card:var(--vscode-sideBar-background,#1e1e1e);
  }
  .bar{margin-bottom:14px;}
  .btn{padding:5px 12px;}
  .btn.primary{background:var(--btn-bg);color:var(--btn-fg);border:none;font-weight:600;}
  .btn.primary:hover{background:var(--btn-hover);}
  .devhead{display:flex;align-items:center;gap:8px;margin-bottom:14px;font-size:12px;color:var(--muted);}
  .devhead .dot{width:8px;height:8px;border-radius:50%;background:var(--ok);flex:none;}
  .devhead .dot.off{background:var(--danger);}
  .group{border:1px solid var(--border);border-radius:8px;margin-bottom:14px;overflow:hidden;background:var(--card);}
  .group-head{padding:9px 14px;font-size:13px;font-weight:600;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:8px;}
  .group-head .icon{font-size:13px;}
  .group-body{padding:12px 14px;}
  .kv{display:grid;grid-template-columns:160px minmax(0,1fr);gap:6px 12px;align-items:baseline;}
  .kv .k{color:var(--muted);font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
  .kv .v{font-size:13px;min-width:0;word-break:break-all;}
  .chips{display:flex;flex-wrap:wrap;gap:6px;}
  .chip{font-size:11px;padding:2px 9px;border:1px solid var(--border);border-radius:10px;background:var(--input-bg);}
  .bar-wrap{display:flex;align-items:center;gap:8px;}
  .bar-track{flex:1;height:8px;border-radius:4px;background:var(--vscode-progressBar-background,#444);overflow:hidden;}
  .bar-fill{height:100%;background:var(--accent);border-radius:4px;}
  .bar-fill.warn{background:var(--warn);}
  .bar-fill.danger{background:var(--danger);}
  .table{width:100%;border-collapse:collapse;font-size:12px;}
  .table th{text-align:left;color:var(--muted);font-weight:500;padding:4px 8px;border-bottom:1px solid var(--border);white-space:nowrap;}
  .table td{padding:4px 8px;border-bottom:1px solid var(--border);vertical-align:middle;}
  .table tr:last-child td{border-bottom:none;}
  .temp{display:inline-block;font-size:12px;padding:2px 10px;border-radius:10px;margin:2px 6px 2px 0;border:1px solid var(--border);}
  .temp.good{color:var(--ok);border-color:var(--ok);}
  .temp.warn{color:var(--warn);border-color:var(--warn);}
  .temp.danger{color:var(--danger);border-color:var(--danger);}
  .temp.nodata{color:var(--muted);border-style:dashed;}
  #error{display:none;padding:12px 14px;border:1px solid var(--danger);color:var(--danger);border-radius:6px;font-size:13px;margin-bottom:14px;}
  @media (max-width:640px){.kv{grid-template-columns:120px minmax(0,1fr);}}
`;

const DI_BODY = (): string => `
<div class="wrap">
  <h1>🖥️ ${t('di.title')}</h1>
  <div class="sub">${t('di.subtitle')}</div>

  <div class="bar">
    <span class="grow"></span>
    <button class="btn" id="copyBtn">${t('di.copy')}</button>
    <button class="btn primary" id="refreshBtn">${t('di.refresh')}</button>
  </div>

  <div id="error"></div>
  <div id="devHead" class="devhead"></div>
  <div id="content"><span class="hint">${t('di.loading')}</span></div>
</div>
<div id="toast"></div>
`;

export class DeviceInfoPanel extends BasePanel {
  static open(extensionUri: vscode.Uri, getApi: () => ApiClient): DeviceInfoPanel {
    return BasePanel.reuse('qpiSdkDeviceInfo', () => new DeviceInfoPanel(extensionUri, getApi));
  }

  private constructor(extensionUri: vscode.Uri, private readonly getApi: () => ApiClient) {
    super({
      viewType: 'qpiSdkDeviceInfo',
      title: () => t('di.title'),
      extensionUri,
      jsFile: 'webview/deviceInfo.js',
      body: DI_BODY,
      style: DI_CSS,
      onMessage: (msg) => this.onMessage(msg),
    });
  }

  private onMessage(msg: any): void {
    if (msg.type === 'refresh') {
      void this.load();
    }
  }

  private async load(): Promise<void> {
    try {
      const data: DeviceInfoData = await this.getApi().deviceInfo();
      this.post('data', data);
    } catch (e) {
      this.post('error', this.errText(e));
    }
  }
}
