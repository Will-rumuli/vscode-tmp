/* 连接向导面板: 选择 local/ssh 模式 -> 填写连接参数 -> 返回 ConnConfig。
   基于 BasePanel, 与其它独立面板共用创建/单例/HTML 骨架。 */
import * as vscode from 'vscode';
import { ConnConfig } from './api';
import { t } from './i18n';
import { BasePanel, escAttr } from './webview/base';

export interface ConnectResult {
  conn: ConnConfig;
}

/** 连接向导特有样式 (覆盖公共 body/wrap 布局 + 表单组件) */
const CONNECT_CSS = `
  body{display:flex;justify-content:center;padding:28px 32px;}
  .wrap{max-width:620px;}
  .field label .req{color:var(--error);margin-left:2px;}
  .row{display:flex;gap:10px;}
  .row .field{flex:1;}
  .radio-group{display:flex;gap:16px;margin-bottom:10px;}
  .radio{display:flex;align-items:center;gap:6px;cursor:pointer;font-size:13px;}
  .radio input{cursor:pointer;}
  .actions{display:flex;gap:10px;justify-content:flex-end;margin-top:6px;}
  button.secondary{padding:8px 16px;font-size:13px;}
  button.primary{padding:8px 20px;font-size:13px;}
`;

const CONNECT_BODY = (): string => `
<div class="wrap">
  <h1>🔌 ${t('connect.title')}</h1>
  <div class="sub">${t('connect.subtitle')}</div>

  <div class="errbox" id="err"></div>

  <div class="section">
    <div class="section-title">${t('connect.sectionMode')}</div>
    <div class="radio-group">
      <label class="radio"><input type="radio" name="mode" value="local" checked> ${t('connect.modeLocal')}</label>
      <label class="radio"><input type="radio" name="mode" value="ssh"> ${t('connect.modeSsh')}</label>
    </div>
    <div class="hint">${t('connect.modeHint')}</div>
  </div>

  <div class="section" id="localSection">
    <div class="section-title">${t('connect.sectionLocal')}</div>
    <div class="field">
      <label>${t('connect.sdkRoot')} <span class="req">*</span></label>
      <div class="inputwrap">
        <input class="input" id="localSdkRoot" placeholder="${escAttr(t('connect.localSdkRootPh'))}" />
        <button class="browse" id="browseLocal">${t('connect.browse')}</button>
      </div>
      <div class="hint">${t('connect.sdkRootHint')}</div>
    </div>
  </div>

  <div class="section hidden" id="sshSection">
    <div class="section-title">${t('connect.sectionSsh')}</div>
    <div class="row">
      <div class="field" style="flex:2">
        <label>${t('connect.host')} <span class="req">*</span></label>
        <input class="input" id="host" placeholder="192.168.1.100" />
      </div>
      <div class="field">
        <label>${t('connect.port')}</label>
        <input class="input" id="port" type="number" value="22" style="width:90px" />
      </div>
    </div>
    <div class="field">
      <label>${t('connect.user')} <span class="req">*</span></label>
      <input class="input" id="user" placeholder="root" />
    </div>
    <div class="field">
      <label>${t('connect.auth')}</label>
      <div class="radio-group" style="margin-bottom:4px;">
        <label class="radio"><input type="radio" name="auth" value="password" checked> ${t('connect.authPwd')}</label>
        <label class="radio"><input type="radio" name="auth" value="key"> ${t('connect.authKey')}</label>
      </div>
    </div>
    <div class="field" id="pwdField">
      <label>${t('connect.password')} <span class="req">*</span></label>
      <input class="input" id="password" type="password" placeholder="${escAttr(t('connect.pwdPh'))}" />
    </div>
    <div class="field hidden" id="keyField">
      <label>${t('connect.keyPath')} <span class="req">*</span></label>
      <div class="inputwrap">
        <input class="input" id="keyPath" placeholder="~/.ssh/id_rsa" />
        <button class="browse" id="browseKey">${t('connect.browse')}</button>
      </div>
    </div>

    <div class="section-title" style="margin-top:18px;">${t('connect.sectionRemoteSdk')}</div>
    <div class="field">
      <label>${t('connect.sdkRootRemote')} <span class="req">*</span></label>
      <input class="input" id="sdkRoot" placeholder="/opt/qpi-sdk" />
      <div class="hint">${t('connect.sdkRootRemoteHint')}</div>
    </div>

    <div class="section-title" style="margin-top:18px;">${t('connect.sectionDeviceLoc')}</div>
    <div class="radio-group">
      <label class="radio"><input type="radio" name="deviceLoc" value="local" checked> ${t('connect.deviceLocLocal')}</label>
      <label class="radio"><input type="radio" name="deviceLoc" value="remote"> ${t('connect.deviceLocRemote')}</label>
    </div>
    <div class="hint">${t('connect.deviceLocHint')}</div>
  </div>

  <div class="actions">
    <button class="secondary" id="cancel">${t('connect.cancel')}</button>
    <button class="primary" id="connectBtn">${t('connect.submit')}</button>
  </div>
</div>
`;

export class ConnectPanel extends BasePanel {
  private resolveFn?: (r: ConnectResult | undefined) => void;

  static open(extensionUri: vscode.Uri, last?: ConnConfig): Promise<ConnectResult | undefined> {
    const p = BasePanel.reuse('qpiSdkConnect', () => new ConnectPanel(extensionUri));
    p.postInit(last);
    return new Promise<ConnectResult | undefined>((resolve) => {
      p.resolveFn = resolve;
    });
  }

  private constructor(extensionUri: vscode.Uri) {
    super({
      viewType: 'qpiSdkConnect',
      title: () => t('connect.title'),
      extensionUri,
      jsFile: 'webview/connect.js',
      body: CONNECT_BODY,
      style: CONNECT_CSS,
      onMessage: (msg) => this.onMessage(msg),
    });
  }

  private postInit(last?: ConnConfig): void {
    // 注意: connect.js 读取 m.payload.last / m.payload.defaults, 需嵌套 payload
    this.post('init', {
      payload: {
        last: last ?? null,
        defaults: { host: '192.168.1.100', user: 'root', port: 22, keyPath: '~/.ssh/id_rsa' },
      },
    });
  }

  private onMessage(msg: any): void {
    switch (msg.type) {
      case 'submit':
        this.submit(msg.payload);
        break;
      case 'cancel':
        this.dispose();
        break;
      case 'pickFolder': {
        const isKey = msg.field === 'keyPath';
        if (isKey) {
          const opts: vscode.OpenDialogOptions = { canSelectFiles: true, canSelectFolders: false, canSelectMany: false, openLabel: t('connect.authKey') };
          vscode.window.showOpenDialog(opts).then((uris) => {
            if (uris?.length) { this.post('pickFolderResult', { field: msg.field, path: uris[0].fsPath }); }
          });
        } else if (msg.field === 'localSdkRoot') {
          vscode.window.showOpenDialog({ canSelectFolders: true, canSelectFiles: false, canSelectMany: false, openLabel: t('connect.sdkRoot') }).then((uris) => {
            if (uris?.length) { this.post('pickFolderResult', { field: msg.field, path: uris[0].fsPath }); }
          });
        }
        break;
      }
    }
  }

  private async submit(raw: any): Promise<void> {
    const mode: 'local' | 'ssh' = raw.mode === 'ssh' ? 'ssh' : 'local';
    const sdk_root: string = (mode === 'ssh' ? raw.sdkRoot : raw.localSdkRoot || raw.sdkRoot)?.trim() ?? '';
    if (!sdk_root) { this.post('error', t('connect.errNoSdkRoot')); return; }
    const conn: ConnConfig = { mode, sdk_root };
    if (mode === 'ssh') {
      const host = (raw.host || '').trim();
      const user = (raw.user || '').trim();
      const port = Number(raw.port) || 22;
      if (!host) { this.post('error', t('connect.errNoHost')); return; }
      if (!user) { this.post('error', t('connect.errNoUser')); return; }
      conn.host = host; conn.user = user; conn.port = port;
      const auth: 'password' | 'key' = raw.auth === 'key' ? 'key' : 'password';
      if (auth === 'password') {
        if (!raw.password) { this.post('error', t('connect.errNoPwd')); return; }
        conn.password = raw.password;
      } else {
        const keyPath = (raw.keyPath || '').trim();
        if (!keyPath) { this.post('error', t('connect.errNoKey')); return; }
        conn.key_path = keyPath;
      }
      // 设备连接位置: 决定 adb/AT/烧录/部署在哪个机器上执行 (仅 ssh 有意义)
      conn.device_location = raw.deviceLoc === 'remote' ? 'remote' : 'local';
    }
    this.resolveFn?.({ conn });
    this.resolveFn = undefined;
    this.dispose();
  }
}
