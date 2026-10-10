/* 性能监控面板: 轮询后端 /perf/sample, 用 SVG 折线图实时展示帧率 / CPU / GPU / 温度。
   与设备信息面板同为独立弹窗, 仅需后端 + adb 设备 (无需连接 SDK 编译环境)。 */
import * as vscode from 'vscode';
import { ApiClient, PerfSampleData } from './api';
import { t } from './i18n';
import { BasePanel } from './webview/base';

/** 性能监控面板特有样式 */
const PERF_CSS = `
  :root{
    --warn:var(--vscode-charts-yellow,#e5c07b);
    --danger:var(--vscode-charts-red,#e06c75);
    --card:var(--vscode-sideBar-background,#1e1e1e);
  }
  .bar{flex-wrap:wrap;}
  .devhead{display:flex;align-items:center;gap:8px;font-size:12px;color:var(--muted);}
  .devhead .dot{width:8px;height:8px;border-radius:50%;background:var(--ok);flex:none;}
  .devhead .dot.off{background:var(--danger);}
  .sel{width:auto;padding:4px 8px;font-size:12px;}
  .stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:14px;}
  .stat{border:1px solid var(--border);border-radius:8px;padding:10px 12px;background:var(--card);}
  .stat .lbl{font-size:11px;color:var(--muted);margin-bottom:4px;}
  .stat .val{font-size:18px;font-weight:600;}
  .stat .val .u{font-size:11px;color:var(--muted);font-weight:400;margin-left:3px;}
  .stat .sub{font-size:11px;color:var(--muted);margin-top:3px;}
  .chart{border:1px solid var(--border);border-radius:8px;margin-bottom:14px;background:var(--card);padding:10px 12px 6px;}
  .chart-head{font-size:13px;font-weight:600;display:flex;align-items:baseline;gap:8px;margin-bottom:6px;}
  .chart-head .unit{font-size:11px;color:var(--muted);font-weight:400;}
  .svg{display:block;width:100%;height:160px;}
  .svg .grid{stroke:var(--border);stroke-width:1;stroke-dasharray:3 3;opacity:.6;}
  .svg .axis{fill:var(--muted);font-size:10px;}
  .svg .hoverG line{stroke:var(--muted);stroke-width:1;stroke-dasharray:3 3;opacity:.85;}
  #tip{position:fixed;z-index:99;pointer-events:none;background:var(--vscode-editorHoverWidget-background,#252526);border:1px solid var(--vscode-editorHoverWidget-border,#454545);border-radius:6px;padding:6px 9px;font-size:11px;line-height:1.6;box-shadow:0 4px 14px rgba(0,0,0,.4);white-space:nowrap;color:var(--fg);}
  #tip .tt{color:var(--muted);margin-bottom:3px;}
  #tip .tr{display:flex;align-items:center;gap:6px;}
  #tip .tr i{width:8px;height:8px;border-radius:2px;flex:none;}
  #tip .tr b{margin-left:auto;padding-left:12px;font-weight:600;}
  .legend{display:flex;flex-wrap:wrap;gap:12px;margin:6px 0 4px;}
  .legend .lg{font-size:11px;color:var(--muted);display:flex;align-items:center;gap:5px;}
  .legend .lg i{width:9px;height:3px;border-radius:2px;display:inline-block;flex:none;}
  .legend .lg b{color:var(--fg);font-weight:600;}
  #error{display:none;padding:12px 14px;border:1px solid var(--danger);color:var(--danger);border-radius:6px;font-size:13px;margin-bottom:14px;}
`;

const PERF_BODY = (): string => `
<div class="wrap">
  <h1>📈 ${t('perf.title')}</h1>
  <div class="sub">${t('perf.subtitle')}</div>

  <div class="bar">
    <span class="devhead" id="devHead"></span>
    <span class="grow"></span>
    <label class="hint">${t('perf.interval')}</label>
    <select class="input sel" id="ivl">
      <option value="1000">1s</option>
      <option value="2000">2s</option>
      <option value="5000">5s</option>
    </select>
    <button class="btn" id="pauseBtn">${t('perf.pause')}</button>
  </div>

  <div id="error"></div>
  <div id="stats" class="stats"></div>
  <div id="charts"></div>
  <div id="tip" class="hidden"></div>
</div>
`;

export class PerfPanel extends BasePanel {
  static open(extensionUri: vscode.Uri, getApi: () => ApiClient): PerfPanel {
    return BasePanel.reuse('qpiSdkPerfMonitor', () => new PerfPanel(extensionUri, getApi));
  }

  private constructor(extensionUri: vscode.Uri, private readonly getApi: () => ApiClient) {
    super({
      viewType: 'qpiSdkPerfMonitor',
      title: () => t('perf.title'),
      extensionUri,
      jsFile: 'webview/perf.js',
      body: PERF_BODY,
      style: PERF_CSS,
      onMessage: (msg) => this.onMessage(msg),
    });
  }

  private onMessage(msg: any): void {
    if (msg.type === 'poll') {
      void this.sample();
    }
  }

  private async sample(): Promise<void> {
    try {
      const data: PerfSampleData = await this.getApi().perfSample();
      this.post('data', data);
    } catch (e) {
      this.post('error', this.errText(e));
    }
  }
}