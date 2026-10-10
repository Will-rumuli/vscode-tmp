/* AT 指令面板前端逻辑: 自定义串口下拉 -> 指令输入 -> 发送; 日志区实时展示
   所有文案/校验由扩展宿主 (atpanel.ts) 本地化后通过 'log' 事件下发, 这里只负责渲染。
   下拉框为自绘组件 (按钮+列表), 展开面板也走主题色, 避免原生 select 白底灰字。 */
const vscode = acquireVsCodeApi();

const portBtn = document.getElementById('portBtn');
const portLabel = document.getElementById('portLabel');
const portList = document.getElementById('portList');
const refreshBtn = document.getElementById('refreshBtn');
const cmdInput = document.getElementById('cmdInput');
const sendBtn = document.getElementById('sendBtn');
const stopBtn = document.getElementById('stopBtn');
const log = document.getElementById('log');

let PORT_LIST = [];
let CURRENT = -1;
let BUSY = false;

/* 串口参数下拉: 选项/默认值在前端维护, 选中值即后端帧参数。
   parity: N/O/E/M/S = None/Odd/Even/Mark/Space; flow: none/sw/hw = 无/软/硬流控 */
const SERIAL_PARAMS = [
  { id: 'baud', def: 115200, items: [4800, 9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600].map((v) => ({ v, t: String(v) })) },
  { id: 'bytesize', def: 8, items: [4, 5, 6, 7, 8].map((v) => ({ v, t: String(v) })) },
  { id: 'parity', def: 'N', items: [{ v: 'N', t: 'None' }, { v: 'O', t: 'Odd' }, { v: 'E', t: 'Even' }, { v: 'M', t: 'Mark' }, { v: 'S', t: 'Space' }] },
  { id: 'stopbits', def: 1, items: [{ v: 1, t: '1' }, { v: 1.5, t: '1.5' }, { v: 2, t: '2' }] },
  { id: 'flow', def: 'none', items: [{ v: 'none', t: 'No Ctrl Flow' }, { v: 'sw', t: 'SW Ctrl Flow' }, { v: 'hw', t: 'HW Ctrl Flow' }] },
];
const SERIAL = {};

function appendLog(line) {
  const div = document.createElement('div');
  div.className = 'log-line ' + classify(line);
  div.textContent = line;
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
}

function classify(line) {
  if (/^\[(系统|完成|System|Done)/.test(line)) return 'sys';
  if (/^\[(错误|失败|Error|Failed)/.test(line)) return 'err';
  if (/>>>\s*发送/.test(line)) return 'tx';
  if (/串口(打开成功|已关闭)/.test(line)) return 'ok';
  if (/^=====/.test(line)) return 'bar';
  return '';
}

function renderPorts(ports, noPortsHint) {
  PORT_LIST = ports || [];
  portList.innerHTML = '';
  if (PORT_LIST.length === 0) {
    CURRENT = -1;
    portLabel.textContent = noPortsHint || '';
    return;
  }
  PORT_LIST.forEach((p, i) => {
    const li = document.createElement('li');
    li.textContent = p.device + (p.desc ? ' — ' + p.desc : '');
    li.addEventListener('click', () => selectPort(i));
    portList.appendChild(li);
  });
  selectPort(0);
}

function selectPort(i) {
  CURRENT = i;
  label.textContent = PORT_LIST[i] ? (PORT_LIST[i].device + (PORT_LIST[i].desc ? ' — ' + PORT_LIST[i].desc : '')) : '';
  closeLists();
}

function selectedPort() {
  return (CURRENT >= 0 && CURRENT < PORT_LIST.length) ? PORT_LIST[CURRENT] : null;
}

function closeLists() {
  document.querySelectorAll('.dropdown-list.open').forEach((el) => el.classList.remove('open'));
}

function openOnly(list) {
  document.querySelectorAll('.dropdown-list.open').forEach((el) => {
    if (el !== list) { el.classList.remove('open'); }
  });
  list.classList.toggle('open');
}

/* 生成 5 个串口参数下拉, 选中值记入 SERIAL (发送时随 payload 一并上交) */
function buildParams() {
  SERIAL_PARAMS.forEach((spec) => {
    const btn = document.getElementById(spec.id + 'Btn');
    const label = document.getElementById(spec.id + 'Label');
    const list = document.getElementById(spec.id + 'List');
    const def = spec.items.find((it) => it.v === spec.def) || spec.items[0];
    SERIAL[spec.id] = def.v;
    label.textContent = def.t;
    spec.items.forEach((it) => {
      const li = document.createElement('li');
      li.textContent = it.t;
      li.addEventListener('click', (e) => {
        e.stopPropagation();
        SERIAL[spec.id] = it.v;
        label.textContent = it.t;
        closeLists();
      });
      list.appendChild(li);
    });
    btn.addEventListener('click', (e) => { e.stopPropagation(); openOnly(list); });
  });
}

portBtn.addEventListener('click', (e) => { e.stopPropagation(); openOnly(portList); });
document.addEventListener('click', closeLists);

refreshBtn.addEventListener('click', () => vscode.postMessage({ type: 'refreshPorts' }));

function setBusy(b) {
  BUSY = b;
  sendBtn.disabled = b;
  stopBtn.disabled = !b;
}

function stopSend() {
  if (!BUSY) return;
  vscode.postMessage({ type: 'stop' });
}

function sendCmd() {
  if (BUSY) return;
  const p = selectedPort();
  const cmd = cmdInput.value.trim();
  // 串口帧参数随每次发送一起上交 (baud/bytesize/parity/stopbits/flow)
  const framing = Object.assign({}, SERIAL);
  if (!p || !cmd) {
    // 校验失败由宿主下发本地化错误提示; 不置 BUSY
    vscode.postMessage({ type: 'send', payload: Object.assign({ port: p ? p.device : '', adb_serial: p ? p.adb_serial || '' : '', cmd }, framing) });
    return;
  }
  setBusy(true);
  vscode.postMessage({ type: 'send', payload: Object.assign({ port: p.device, adb_serial: p.adb_serial || '', cmd }, framing) });
  cmdInput.value = '';
}

cmdInput.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendCmd(); }
});
sendBtn.addEventListener('click', sendCmd);
stopBtn.addEventListener('click', stopSend);

window.addEventListener('message', (e) => {
  const m = e.data;
  switch (m.type) {
    case 'ports':
      renderPorts(m.payload.ports, m.payload.noPortsHint);
      break;
    case 'log':
      if (m.payload && typeof m.payload.line === 'string') {
        appendLog(m.payload.line);
      }
      if (m.payload && m.payload.done) { setBusy(false); }
      break;
  }
});

// 首次打开即构建串口参数下拉并扫描串口
buildParams();
vscode.postMessage({ type: 'ready' });
