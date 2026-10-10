/* 设备树配置面板: 展示板级设备树可配置节点, 支持启用/禁用/编辑属性/定位源码。
   基于 BasePanel, 数据来自后端 /device-tree 系列接口。 */
import * as vscode from 'vscode';
import { ApiClient, DeviceTreeData } from './api';
import { t } from './i18n';
import { BasePanel, escAttr } from './webview/base';

/** 设备树面板特有样式 */
const DT_CSS = `
  #treePath{color:var(--muted);font-size:12px;margin:0 0 12px;word-break:break-all;}
  #nodeList{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;}
  .node-item{min-width:0;display:flex;flex-direction:column;gap:8px;padding:10px 12px;border:1px solid var(--border);border-radius:6px;background:var(--vscode-sideBar-background,#1e1e1e);}
  .node-item:hover{border-color:color-mix(in srgb, var(--accent) 45%, transparent);}
  .node-main{min-width:0;}
  .node-path{display:block;font-size:12px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:var(--fg);}
  .node-props{display:block;font-size:11px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:3px;}
  .node-actions{display:flex;align-items:center;gap:6px;flex-wrap:wrap;}
  .badge{font-size:10px;padding:1px 7px;border-radius:8px;border:1px solid var(--border);}
  .badge.on{color:var(--ok);border-color:var(--ok);}
  .badge.off{color:var(--muted);}
  #propEditor{margin-top:14px;padding:14px 16px;border:1px solid var(--border);border-radius:6px;background:var(--vscode-sideBar-background,#1e1e1e);}
  .editor-title{font-size:13px;font-weight:600;margin-bottom:8px;}
  #propText{width:100%;min-height:110px;box-sizing:border-box;background:var(--input-bg);color:var(--input-fg);border:1px solid var(--input-border);border-radius:4px;padding:8px;font-family:var(--vscode-editor-font-family,Consolas,monospace);font-size:12px;line-height:1.6;outline:none;resize:vertical;}
  .editor-actions{display:flex;gap:8px;align-items:center;margin-top:10px;}
  @media (max-width:720px){#nodeList{grid-template-columns:minmax(0,1fr);}}
`;

const DT_BODY = (): string => `
<div class="wrap">
  <h1>&#128295; ${t('dt.titleHeader')}</h1>
  <div class="sub">${t('dt.subtitle')}</div>

  <div class="bar">
    <div class="dropdown" id="srcDropdown">
      <button type="button" class="input dropdown-btn" id="srcBtn"><span id="srcLabel">${t('dt.loading')}</span><span class="caret">&#9662;</span></button>
      <ul class="dropdown-list" id="srcList"></ul>
    </div>
    <button class="browse" id="refreshBtn">${t('dt.refresh')}</button>
  </div>

  <div id="treePath"></div>

  <div class="bar">
    <input class="input mono" id="filterInput" placeholder="${escAttr(t('dt.filterPh'))}" />
  </div>

  <div id="nodeList"></div>

  <div id="propEditor" class="hidden">
    <div class="editor-title" id="propEditorTitle">${t('dt.editorTitle')}</div>
    <textarea id="propText" placeholder="${escAttr(t('dt.propPh'))}"></textarea>
    <div class="editor-actions">
      <button class="primary" id="savePropsBtn">${t('dt.save')}</button>
      <button class="btn" id="cancelPropsBtn">${t('btn.cancel')}</button>
      <span class="hint">${t('dt.editorHint')}</span>
    </div>
  </div>
</div>
<div id="toast"></div>
`;

export class DeviceTreePanel extends BasePanel {
  static open(extensionUri: vscode.Uri, getApi: () => ApiClient): DeviceTreePanel {
    return BasePanel.reuse('qpiSdkDeviceTree', () => new DeviceTreePanel(extensionUri, getApi));
  }

  private constructor(extensionUri: vscode.Uri, private readonly getApi: () => ApiClient) {
    super({
      viewType: 'qpiSdkDeviceTree',
      title: () => t('dt.title'),
      extensionUri,
      jsFile: 'webview/devicetree.js',
      body: DT_BODY,
      style: DT_CSS,
      onMessage: (msg) => this.onMessage(msg),
    });
  }

  private onMessage(msg: any): void {
    switch (msg.type) {
      case 'refresh':
        void this.load();
        break;
      case 'toggle':
        void this.toggle(msg);
        break;
      case 'saveProperties':
        void this.saveProperties(msg);
        break;
    }
  }

  private async load(): Promise<void> {
    try {
      const data: DeviceTreeData = await this.getApi().deviceTree();
      this.post('data', data);
    } catch (e) {
      this.post('error', this.errText(e));
    }
  }

  private async toggle(msg: { node: string; enabled: boolean; source: string }): Promise<void> {
    try {
      await this.getApi().setDeviceTreeStatus(msg.node, msg.enabled, msg.source);
      this.post('status-result', { ok: true, node: msg.node, enabled: msg.enabled, source: msg.source });
    } catch (e) {
      this.post('status-result', {
        ok: false, node: msg.node, enabled: msg.enabled,
        message: this.errText(e),
      });
    }
  }

  private async saveProperties(msg: { node: string; source: string; properties: string }): Promise<void> {
    try {
      await this.getApi().setDeviceTreeProperties(msg.node, msg.source, msg.properties);
      this.post('properties-result', { ok: true, node: msg.node });
    } catch (e) {
      this.post('properties-result', {
        ok: false, node: msg.node,
        message: this.errText(e),
      });
    }
  }
}
