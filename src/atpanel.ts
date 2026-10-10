/* AT 指令面板: 选择串口 -> 输入 AT 指令 -> 发送, 实时显示串口日志。
   文案由宿主本地化后通过 'log' 事件下发, 面板不注入内联 i18n 字典 (unsafeInline=false)。 */
import * as vscode from 'vscode';
import { ApiClient } from './api';
import { t } from './i18n';
import { BasePanel } from './webview/base';

/** AT 面板特有样式 */
const AT_CSS = `
  :root{--log-bg:var(--vscode-terminal-background,#1e1e1e);}
  .wrap{max-width:720px;}
  .inputwrap .input{flex:1;}
  .params{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin-bottom:14px;}
  .param label{display:block;font-size:12px;color:var(--muted);margin-bottom:5px;}
  .param .dropdown{min-width:0;}
  .dropdown{flex:1;min-width:0;}
  .dropdown-btn{display:flex;align-items:center;justify-content:space-between;gap:8px;cursor:pointer;text-align:left;padding-right:9px;}
  .dropdown-btn .caret{color:var(--muted);font-size:10px;flex:none;}
  button.primary{padding:8px 20px;font-size:13px;white-space:nowrap;}
  #log{background:var(--log-bg);border:1px solid var(--border);border-radius:4px;height:280px;overflow-y:auto;padding:8px 10px;font-family:var(--vscode-editor-font-family,Consolas,monospace);font-size:12px;line-height:1.5;}
  .log-line{white-space:pre-wrap;word-break:break-all;}
  .log-line.sys{color:var(--muted);}
  .log-line.tx{color:var(--vscode-charts-blue,#3794ff);}
  .log-line.ok{color:var(--vscode-charts-green,#89d185);}
  .log-line.err{color:var(--error);}
  .log-line.bar{color:var(--muted);}
`;

/** 串口参数下拉: 自绘组件 (复用 base 的 .dropdown 样式), 选项与选中值由 at.js 填充 */
const PARAM = (id: string, label: string): string => `
      <div class="param">
        <label>${label}</label>
        <div class="dropdown" id="${id}Dropdown">
          <button type="button" class="input dropdown-btn" id="${id}Btn"><span id="${id}Label"></span><span class="caret">&#9662;</span></button>
          <ul class="dropdown-list" id="${id}List"></ul>
        </div>
      </div>`;

const AT_BODY = (): string => `
<div class="wrap">
  <h1>🔌 ${t('at.title')}</h1>
  <div class="sub">${t('at.subtitle')}</div>

  <div class="section">
    <div class="field">
      <label>${t('at.port')}</label>
      <div class="inputwrap">
        <div class="dropdown" id="portDropdown">
          <button type="button" class="input dropdown-btn" id="portBtn"><span id="portLabel">${t('at.scanning')}</span><span class="caret">&#9662;</span></button>
          <ul class="dropdown-list" id="portList"></ul>
        </div>
        <button class="browse" id="refreshBtn">${t('at.refresh')}</button>
      </div>
    </div>
    <div class="section-title">${t('at.serial')}</div>
    <div class="params">
      ${PARAM('baud', t('at.baud'))}
      ${PARAM('bytesize', t('at.bytesize'))}
      ${PARAM('parity', t('at.parity'))}
      ${PARAM('stopbits', t('at.stopbits'))}
      ${PARAM('flow', t('at.flow'))}
    </div>
    <div class="field">
      <label>${t('at.cmd')}</label>
      <div class="inputwrap">
        <input class="input" id="cmdInput" placeholder="${t('at.cmdPh')}" />
        <button class="primary" id="sendBtn">${t('at.send')}</button>
        <button class="browse" id="stopBtn" disabled>${t('at.stop')}</button>
      </div>
    </div>
  </div>

  <div class="section">
    <div class="section-title">${t('at.log')}</div>
    <div id="log"></div>
  </div>
</div>
`;

export class ATPanel extends BasePanel {
  static open(extensionUri: vscode.Uri, getApi: () => ApiClient): ATPanel {
    return BasePanel.reuse('qpiSdkAt', () => new ATPanel(extensionUri, getApi));
  }

  private constructor(extensionUri: vscode.Uri, private readonly getApi: () => ApiClient) {
    super({
      viewType: 'qpiSdkAt',
      title: () => t('at.title'),
      extensionUri,
      jsFile: 'webview/at.js',
      body: AT_BODY,
      style: AT_CSS,
      unsafeInline: false,
      onMessage: (msg) => this.onMessage(msg),
    });
  }

  private curTaskId: string | null = null;

  private onMessage(msg: any): void {
    switch (msg.type) {
      case 'ready':
      case 'refreshPorts':
        void this.loadPorts();
        break;
      case 'send':
        void this.send(msg.payload);
        break;
      case 'stop':
        void this.stopSend();
        break;
      case 'close':
        this.dispose();
        break;
    }
  }

  private async loadPorts(): Promise<void> {
    this.post('log', { line: t('at.scanning') });
    try {
      const { ports } = await this.getApi().ports();
      this.post('ports', { ports, noPortsHint: t('at.noPorts') });
      this.post('log', { line: t('at.loaded', { n: String(ports.length) }) });
    } catch (e) {
      this.post('log', { line: t('at.errScan', { err: this.errText(e) }) });
    }
  }

  private async send(payload: { port: string; adb_serial?: string; cmd: string; baud?: number;
    bytesize?: number; parity?: string; stopbits?: number; flow?: string }): Promise<void> {
    const { port, adb_serial, cmd } = payload || {};
    const baud = Number(payload?.baud) || 115200;
    const bytesize = Number(payload?.bytesize) || 8;
    const parity = payload?.parity || 'N';
    const stopbits = Number(payload?.stopbits) || 1;
    const flow = payload?.flow || 'none';
    if (!port) {
      this.post('log', { line: t('at.errNoPort') });
      return;
    }
    const cmds = String(cmd).split(';').map((s) => s.trim()).filter(Boolean);
    if (cmds.length === 0) {
      this.post('log', { line: t('at.errNoCmd') });
      return;
    }
    this.post('log', { line: t('at.sendStart', {
      port, baud: String(baud), framing: `${bytesize}${parity}${stopbits}`, flow }) });
    let tid = '';
    // finish() 保证"结束"只收尾一次, 防止超时兜底与后端 done 事件重复收尾
    let finished = false;
    const finish = (...lines: string[]) => {
      if (finished) { return; }
      finished = true;
      for (const line of lines) { this.post('log', { line, done: true }); }
    };
    try {
      const task = await this.getApi().at(cmds, port, adb_serial || '', baud,
                                          bytesize, parity, stopbits, flow);
      tid = this.curTaskId = task.task_id;
      // 总超时兜底: 设备/串口无响应时任务可能永远挂起, 超时后主动杀掉,
      // 保证前端 BUSY 状态能复位 (对应 at.js 的发送按钮)。
      const timeoutMs = 30_000 + cmds.length * 10_000;
      await this.raceTimeout(
        this.getApi().stream(
          task.task_id,
          (ev) => {
            if (ev.type === 'out' || ev.type === 'err') {
              this.post('log', { line: ev.line });
            } else if (ev.type === 'done') {
              if (ev.status === 'killed') {
                // 被停止/超时杀掉: 不显示"退出码 null"
                finish(t('at.stopped'), t('at.sendEnd'));
                return;
              }
              const line = ev.exit_code === 0
                ? t('at.done', { code: String(ev.exit_code) })
                : t('at.fail', { code: String(ev.exit_code) });
              finish(line, t('at.sendEnd'));
            }
          },
          (err: string) => finish(t('at.errSend', { err })),
        ),
        timeoutMs,
        async () => {
          try { await this.getApi().killTask(task.task_id); } catch { /* ignore */ }
          finish(t('at.timeoutKilled'), t('at.sendEnd'));
        },
      );
    } catch (e) {
      finish(t('at.errSend', { err: this.errText(e) }));
    } finally {
      if (this.curTaskId === tid) { this.curTaskId = null; }
    }
  }

  /** 停止当前发送: 杀掉后端任务, 由 SSE done(killed) 事件统一收尾。 */
  private async stopSend(): Promise<void> {
    const id = this.curTaskId;
    if (!id) { return; }
    try { await this.getApi().killTask(id); } catch { /* ignore */ }
  }

  /** 给 Promise 加超时兜底: 超时后执行 onTimeout 并结束等待。 */
  private raceTimeout<T>(
    p: Promise<T>,
    ms: number,
    onTimeout: () => Promise<void> | void,
  ): Promise<T | void> {
    let timer: ReturnType<typeof setTimeout> | undefined;
    return Promise.race([
      p,
      new Promise<void>((resolve) => {
        timer = setTimeout(async () => { await onTimeout(); resolve(); }, ms);
      }),
    ]).finally(() => { if (timer) { clearTimeout(timer); } });
  }
}
