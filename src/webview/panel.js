/* QPi M2 SDK 控制台 webview 前端逻辑
 * 通过 vscode.postMessage 与 extension 通信, extension 再调后端 API
 * 文案通过扩展注入的 window.QPI_I18N (当前语言完整字典) 本地化 */
'use strict';

// @ts-ignore
const vscode = acquireVsCodeApi();

const $ = (id) => document.getElementById(id);

/* ================= 中英文本地化 =================
 * 语言随 IDE 系统语言切换: 扩展在 HTML 里注入 window.QPI_I18N,
 * 这里统一按 key 取当前语言文案; 静态 HTML 元素用 data-i18n 等属性本地化。 */
const I18N = window.QPI_I18N || {};
function tr(key, vars) {
  let s = I18N[key] || key;
  if (vars) {
    for (const k in vars) {
      s = s.split('{' + k + '}').join(String(vars[k]));
    }
  }
  return s;
}
function localizeStatic() {
  document.querySelectorAll('[data-i18n]').forEach((el) => { el.textContent = tr(el.dataset.i18n); });
  document.querySelectorAll('[data-i18n-html]').forEach((el) => { el.innerHTML = tr(el.dataset.i18nHtml); });
  document.querySelectorAll('[data-i18n-ph]').forEach((el) => { el.placeholder = tr(el.dataset.i18nPh); });
  document.querySelectorAll('[data-i18n-title]').forEach((el) => { el.title = tr(el.dataset.i18nTitle); });
}

const state = {
  connected: false,
  conn: null,
  sdkRoot: '',
  projectDir: 'projects',
  tasks: {},          // taskId -> { running, log }
  logs: {},           // 日志窗口 id -> { tab, panel, title }
  activeLog: null,
  activeOutput: null,
  templates: [],
  projects: [],
  examples: [],
  flashTools: [],
  platform: null,     // SDK 平台标识 (H1/Qualcomm 或 M2/Rockchip), 由 status / flash-tools 返回
  deviceTreeNodes: [],
  deviceTreeSources: [],
  deviceTreeEditing: null,
  devices: [],
  components: [],
  repos: {},          // 组件 -> git url (从配置加载)
  selectedTemplate: null,
  tplFilter: 'all',
};

/* ================= 通用 ================= */
function toast(msg, type = '') {
  const el = $('toast');
  el.textContent = msg;
  el.className = 'toast ' + type;
  el.classList.remove('hidden');
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.add('hidden'), 3500);
}

function post(msg) { vscode.postMessage(msg); }

/* 操作 -> 本地化标签 (用于任务开始/结束提示) */
function methodLabel(m) {
  const key = 'pjs.method.' + m;
  return I18N[key] ? tr(key) : m;
}

function setBadge(id, text, cls) {
  const el = $(id);
  el.textContent = text;
  el.className = 'badge ' + (cls || 'badge-off');
}

function esc(s) {
  return String(s ?? '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

/* ================= Tab 切换 ================= */
document.querySelectorAll('.tab').forEach((tab) => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach((t) => t.classList.remove('active'));
    document.querySelectorAll('.tabpage').forEach((p) => p.classList.remove('active'));
    tab.classList.add('active');
    $('tab-' + tab.dataset.tab).classList.add('active');
    if (tab.dataset.tab === 'project' && state.connected) { refreshProjects(); }
    if (tab.dataset.tab === 'device-tree' && state.connected) { refreshDeviceTree(); }
    if (tab.dataset.tab === 'repo' && state.connected) { post({ type: 'repo-status', repos: state.repos }); }
  });
});

/* ================= 连接管理 ================= */
function currentConn() {
  const mode = document.querySelector('input[name="mode"]:checked').value;
  const conn = {
    mode,
    sdk_root: $('inSdkRoot').value.trim(),
    project_dir: $('inProjectDir').value.trim() || 'projects',
  };
  if (mode === 'ssh') {
    conn.host = $('inHost').value.trim();
    conn.port = parseInt($('inPort').value || '22', 10);
    conn.user = $('inUser').value.trim();
    conn.password = $('inPass').value || undefined;
    conn.key_path = $('inKeyPath').value.trim() || undefined;
  }
  return conn;
}

function applyConn(conn) {
  if (!conn) { return; }
  document.querySelector(`input[name="mode"][value="${conn.mode || 'local'}"]`).checked = true;
  $('inSdkRoot').value = conn.sdk_root || '';
  $('inProjectDir').value = conn.project_dir || 'projects';
  if (conn.mode === 'ssh') {
    $('inHost').value = conn.host || '';
    $('inPort').value = conn.port || 22;
    $('inUser').value = conn.user || '';
    $('inPass').value = conn.password || '';
    $('inKeyPath').value = conn.key_path || '';
  }
  updateModeUi(conn.mode || 'local');
}

function updateModeUi(mode) {
  const isSsh = mode === 'ssh';
  $('sshFields').classList.toggle('hidden', !isSsh);
  $('btnBrowseSdk').classList.toggle('hidden', isSsh);
  $('inSdkRoot').placeholder = isSsh
    ? tr('panel.sdkRootPhSsh')
    : tr('panel.sdkRootPh');
}

document.querySelectorAll('input[name="mode"]').forEach((r) => {
  r.addEventListener('change', () => updateModeUi(r.value));
});

$('btnConnect').addEventListener('click', () => {
  const conn = currentConn();
  if (!conn.sdk_root) { toast(tr('pjs.errSdkRoot'), 'error'); return; }
  $('btnConnect').disabled = true;
  $('btnConnect').textContent = tr('pjs.connecting');
  post({ type: 'connect', conn });
  post({ type: 'save-connection', conn });
});

$('btnDisconnect').addEventListener('click', () => {
  post({ type: 'disconnect' });
});

$('btnBrowseSdk').addEventListener('click', () => {
  post({ type: 'browse-sdk' });
});

$('btnRefreshDevices').addEventListener('click', () => {
  post({ type: 'devices' });
});

$('btnBackendInfo').addEventListener('click', () => {
  // 设备信息已改为独立结构化面板: 交给宿主打开 (仅需后端与 adb 设备)
  post({ type: 'open-device-info' });
});

$('btnPerfMonitor').addEventListener('click', () => {
  // 性能监控为独立实时面板: 交给宿主打开 (仅需后端与 adb 设备)
  post({ type: 'open-perf-monitor' });
});

/* 设备类操作: 检测到 adb 设备即可执行, 不需要连接 SDK */
const DEVICE_ONLY_METHODS = new Set([
  'adb', 'at', 'dmesg', 'journalctl', 'reboot', 'rebootLoader',
  'screenshot',
  'audioCollect', 'screenRecord', 'tinymix',
  'aplay', 'playVideo',
]);

/* ================= 任务系统 ================= */
function startTask(method, args = []) {
  if (!state.connected && !DEVICE_ONLY_METHODS.has(method)) {
    toast(tr('pjs.needConn'));
    return false;
  }
  // 特殊占位符: __SEL__ 表示当前选中的应用目录
  args = args.map((a) => (a === '__SEL__' ? $('selAppDir').value : a));
  post({ type: 'start-task', method, args });
  return true;
}

/* ================= 设备树 ================= */
function refreshDeviceTree() {
  if (!state.connected) { toast(tr('pjs.needConnShort'), 'error'); return; }
  $('deviceTreeList').innerHTML = `<span class="hint">${tr('pjs.readingDt')}</span>`;
  post({ type: 'device-tree' });
}
function deviceTreeSourceNode(node, sourceId) {
  const source = state.deviceTreeSources.find((item) => item.id === sourceId);
  return source?.nodes.find((item) => item.name === node) || null;
}
function renderDeviceTree() {
  const list = $('deviceTreeList');
  const source = state.deviceTreeSources.find((item) => item.id === $('selDeviceTreeSource').value);
  if (!source || !source.nodes.length) {
    list.innerHTML = `<span class="hint">${tr('dt.noNodes')}</span>`;
    return;
  }
  const filter = $('inDeviceTreeFilter').value.trim().toLowerCase();
  const nodes = source.nodes.filter((node) => !filter ||
    `${node.path || ''} ${node.name} ${JSON.stringify(node.properties || {})}`.toLowerCase().includes(filter));
  list.innerHTML = nodes.map((node) => {
    const canEdit = source.id !== 'compiled-dtb' || !!node.source;
    const preview = Object.entries(node.properties || {})
      .filter(([key]) => !['status', 'phandle', 'linux,phandle'].includes(key))
      .slice(0, 2).map(([key, value]) => `${key}: ${value}`).join(' · ');
    return `
    <div class="device-tree-item">
      <div class="dt-node-main">
        <span class="mono dt-node-path" title="${esc(node.path || '&' + node.name)}">${esc(node.path || '&' + node.name)}</span>
        <span class="dt-properties" title="${esc(preview)}">${esc(preview || tr('dt.noProperties'))}</span>
      </div>
      <div class="dt-node-actions">
        <span class="badge ${node.enabled ? 'badge-on' : 'badge-off'}">${node.enabled ? tr('dt.enabled') : tr('dt.disabled')}</span>
        ${canEdit
          ? `<button type="button" class="btn btn-sm dt-toggle" data-dt-node="${esc(node.name)}" data-dt-enabled="${node.enabled}">${node.enabled ? tr('dt.disable') : tr('dt.enable')}</button><button type="button" class="btn btn-sm dt-edit" data-dt-node="${esc(node.name)}">${tr('dt.properties')}</button>`
          : `<span class="hint">${tr('dt.readOnly')}</span>`}
      </div>
    </div>`;
  }).join('');
  list.querySelectorAll('.dt-toggle').forEach((toggle) => {
    toggle.addEventListener('click', () => {
      const node = toggle.dataset.dtNode;
      const enabled = toggle.dataset.dtEnabled !== 'true';
      toggle.disabled = true;
      toggle.textContent = tr('dt.saving');
      post({ type: 'device-tree-status', node, enabled, source: source.id });
    });
  });
  list.querySelectorAll('.dt-edit').forEach((button) => {
    button.addEventListener('click', (event) => {
      event.preventDefault();
      const finalNode = source.nodes.find((item) => item.name === button.dataset.dtNode);
      // DTB 属性是最终合并结果，可能含布尔属性，不能直接回写到 &label 块。
      // 永远加载已经自动定位的 DTS/DTSI 原始属性。
      const editSource = source.id === 'compiled-dtb' ? finalNode.source : source.id;
      const editNode = source.id === 'compiled-dtb' ? finalNode.sourceLabel : finalNode.name;
      const node = deviceTreeSourceNode(editNode, editSource);
      if (!node) { toast(tr('pjs.noEditSource'), 'error'); return; }
      state.deviceTreeEditing = { node: node.name, source: editSource };
      $('deviceTreeEditorTitle').textContent = tr('pjs.editTitle', { node: node.name });
      $('deviceTreeProperties').value = Object.entries(node.properties || {}).map(([key, value]) => value ? `${key} = ${value};` : `${key};`).join('\n');
      $('deviceTreeEditor').classList.remove('hidden');
      $('deviceTreeProperties').focus();
    });
  });
  list.querySelectorAll('.dt-locate').forEach((button) => {
    button.addEventListener('click', (event) => {
      event.preventDefault();
      button.disabled = true;
      post({ type: 'device-tree-source', node: button.dataset.dtNode, label: button.dataset.dtLabel });
    });
  });
}
$('btnRefreshDeviceTree').addEventListener('click', refreshDeviceTree);
$('selDeviceTreeSource').addEventListener('change', renderDeviceTree);
$('inDeviceTreeFilter').addEventListener('input', renderDeviceTree);
$('btnCancelDeviceTreeProperties').addEventListener('click', () => $('deviceTreeEditor').classList.add('hidden'));
$('btnSaveDeviceTreeProperties').addEventListener('click', () => {
  if (!state.deviceTreeEditing) { return; }
  const button = $('btnSaveDeviceTreeProperties');
  button.disabled = true;
  post({ type: 'device-tree-properties', ...state.deviceTreeEditing, properties: $('deviceTreeProperties').value });
});

// 按钮 data-task 快捷启动
document.querySelectorAll('[data-task]').forEach((btn) => {
  btn.addEventListener('click', () => {
    if (btn.id === 'btnBuildApp') {
      const dir = $('selAppDir').value;
      if (!dir) { toast(tr('pjs.errSelectApp'), 'error'); return; }
      startTask('build', ['app', dir]);
      return;
    }
    const spec = JSON.parse(btn.dataset.task);
    startTask(spec.method, spec.args || []);
  });
});

/* ================= 日志窗口 =================
 * 所有命令输出统一进入当前激活的日志窗口 (默认"主日志")。
 * "➕ 新增窗口"可新建独立日志窗口, 后续输出自动转到新窗口。
 */
let logSeq = 0;
function newLogWindow(title, isMain = false) {
  if (isMain && state.logs.main) { return 'main'; }
  logSeq += 1;
  const id = isMain ? 'main' : 'log' + logSeq;
  const tab = document.createElement('button');
  tab.className = 'log-tab';
  tab.dataset.log = id;
  tab.innerHTML = `<span>${esc(title)}</span>${isMain ? '' : `<span class="close" title="${tr('pjs.closeLogTip')}" data-log="${esc(id)}">✕</span>`}`;
  tab.addEventListener('click', (event) => {
    if (event.target.classList.contains('close')) {
      event.stopPropagation();
      closeLogWindow(id);
      return;
    }
    activateLog(id);
  });
  $('outputTabs').insertBefore(tab, $('btnNewLog'));
  const panel = document.createElement('div');
  panel.className = 'log-panel';
  panel.dataset.log = id;
  $('outputPanels').appendChild(panel);
  state.logs[id] = { tab, panel, title };
  activateLog(id);
  return id;
}
function closeLogWindow(id) {
  if (id === 'main') { return; }
  const log = state.logs[id];
  if (!log) { return; }
  log.tab.remove();
  log.panel.remove();
  delete state.logs[id];
  Object.values(state.tasks).forEach((task) => {
    if (task.log === id) { task.log = 'main'; }
  });
  if (state.activeLog === id) {
    const rest = Object.keys(state.logs);
    activateLog(rest[rest.length - 1] || 'main');
  }
}
function activateLog(id) {
  state.activeLog = id;
  Object.entries(state.logs).forEach(([lid, l]) => {
    const on = lid === id;
    l.tab.classList.toggle('active', on);
    l.panel.classList.toggle('active', on);
    if (on) { l.panel.scrollTop = l.panel.scrollHeight; }
  });
}
function currentLogId() { return state.activeLog || 'main'; }

function appendLog(text, cls = '', logId) {
  const id = logId || currentLogId();
  const l = state.logs[id];
  if (!l) { return; }
  const line = document.createElement('div');
  line.className = cls;
  line.textContent = text;
  l.panel.appendChild(line);
  // 输出行数上限: 防止超大输出 (如 dmesg / logcat 全量) 拖垮 webview
  const MAX_OUTPUT_LINES = 4000;
  while (l.panel.childElementCount > MAX_OUTPUT_LINES) {
    l.panel.removeChild(l.panel.firstElementChild);
  }
  if (l.panel.classList.contains('active')) {
    l.panel.scrollTop = l.panel.scrollHeight;
  }
}

/* 停止最近启动且仍在运行的任务 (类似终端 Ctrl+C) */
function lastRunningTaskId() {
  const ids = Object.keys(state.tasks).filter((tid) => state.tasks[tid].running);
  return ids.length ? ids[ids.length - 1] : null;
}
function updateStopBtn() {
  $('btnStopTask').classList.toggle('hidden', !lastRunningTaskId());
}
$('btnStopTask').addEventListener('click', () => {
  const taskId = lastRunningTaskId();
  if (!taskId) { toast(tr('pjs.noRunningTask'), 'warn'); return; }
  // 立即标记 stopping: 后续该任务的残留输出一律忽略, 界面马上静止,
  // 不等后端把缓冲推完
  if (state.tasks[taskId]) { state.tasks[taskId].stopping = true; }
  toast(tr('pjs.stoppingTask'), 'warn');
  post({ type: 'kill-task', taskId });
});
$('btnNewLog').addEventListener('click', () => {
  const id = newLogWindow(tr('pjs.logN', { n: logSeq + 1 }));
  appendLog(tr('pjs.newLogTitle', { title: state.logs[id].title }), 'warn');
});

function initLogWindows() {
  newLogWindow(tr('pjs.mainLog'), true);
}

/* ================= 日志区缩放 ================= */
let outputCollapsed = false;
let outputLastHeight = 230;
function setOutputHeight(h, remember = true) {
  $('outputSection').style.height = h + 'px';
  if (remember) { outputLastHeight = h; }
}
function toggleOutput() {
  outputCollapsed = !outputCollapsed;
  const btn = $('btnToggleOutput');
  $('outputResizer').style.display = outputCollapsed ? 'none' : '';
  if (outputCollapsed) {
    setOutputHeight(28, false); // 只留 tab 栏, 不覆盖展开前高度
    btn.textContent = tr('pjs.expand');
  } else {
    setOutputHeight(outputLastHeight);
    btn.textContent = tr('pjs.collapse');
  }
}
$('btnToggleOutput').addEventListener('click', toggleOutput);

// 拖动分隔条调整日志区高度 (范围 30px ~ 70% 视口高)
(function initOutputResizer() {
  const rz = $('outputResizer');
  let dragging = false;
  rz.addEventListener('mousedown', (e) => {
    if (outputCollapsed) { return; }
    dragging = true;
    rz.classList.add('dragging');
    e.preventDefault();
  });
  window.addEventListener('mousemove', (e) => {
    if (!dragging) { return; }
    const h = Math.min(Math.max(window.innerHeight - e.clientY, 30),
      Math.floor(window.innerHeight * 0.7));
    setOutputHeight(h);
  });
  window.addEventListener('mouseup', () => {
    if (dragging) { dragging = false; rz.classList.remove('dragging'); }
  });
})();

/* ================= 编译页 ================= */
function refreshAppDirs() {
  const dirs = [
    ...state.projects.map((p) => p.dir),
    ...state.examples.map((e) => `docs/examples/${e}`),
  ];
  const sel = $('selAppDir');
  if (dirs.length === 0) {
    sel.innerHTML = `<option value="">${tr('pjs.noAppDirs')}</option>`;
    return;
  }
  sel.innerHTML = dirs.map((d) => `<option value="${esc(d)}">${esc(d)}</option>`).join('');
}
$('btnRefreshApps').addEventListener('click', () => { post({ type: 'projects' }); post({ type: 'examples' }); });

/* ================= 烧录页 ================= */
function renderAdb() {
  const el = $('adbList');
  const devs = state.devices || [];
  if (devs.length === 0) {
    el.innerHTML = `<div class="adb-empty">${tr('pjs.noAdbDevices')}<br><span class="hint">${tr('pjs.usbDebugHint')}</span></div>`;
  } else {
    el.innerHTML = devs.map((d) => `
      <div class="adb-item">
        <span class="dot ${d.state === 'device' ? 'dot-ok' : 'dot-warn'}"></span>
        <span class="mono">${esc(d.serial)}</span>
        <span class="badge ${d.state === 'device' ? 'badge-on' : 'badge-warn'}">${esc(d.state)}</span>
      </div>`).join('');
  }
  setBadge('devBadge', devs.some((d) => d.state === 'device') ? tr('pjs.deviceOnline') : tr('pjs.noDevices'),
    devs.some((d) => d.state === 'device') ? 'badge-on' : '');
}
$('btnRebootLoader').addEventListener('click', () => startTask('rebootLoader'));

function renderDeploySelect() {
  const sel = $('selDeployProject');
  if (state.projects.length === 0) {
    sel.innerHTML = `<option value="">${tr('pjs.noProjects')}</option>`;
    return;
  }
  sel.innerHTML = state.projects.map((p) =>
    `<option value="${esc(p.name)}">${esc(p.name)}${p.hasBinary ? ' ' + tr('pjs.compiled') : ' ' + tr('pjs.notCompiled')}</option>`).join('');
}
function fillDeploySourceFromProject() {
  const p = state.projects.find((x) => x.name === $('selDeployProject').value);
  if (!p) { return; }
  $('inDeploySource').value = p.hasBinary && p.binary ? `${p.dir}/${p.binary}` : p.dir;
  $('selDeployKind').value = p.hasBinary ? 'file' : 'directory';
}
$('selDeployProject').addEventListener('change', fillDeploySourceFromProject);

function deploy(run) {
  const source = $('inDeploySource').value.trim();
  const target = $('inDeployTarget').value.trim();
  const kind = $('selDeployKind').value;
  const runCmd = $('inDeployRunCmd').value.trim();
  if (!source) { toast(tr('pjs.errSourcePath'), 'error'); return; }
  if (!target || !target.startsWith('/')) { toast(tr('pjs.errTargetPath'), 'error'); return; }
  startTask('deploy', [source, target, kind, run, runCmd]);
}
$('btnDeployRun').addEventListener('click', () => {
  deploy(true);
});
$('btnDeployPush').addEventListener('click', () => {
  deploy(false);
});

/** 烧录工具提示: 由后端 hint_key/hint_vars 渲染, 架构不匹配时追加警告 */
function flashToolHint(t) {
  let s = t.hint_key ? tr(t.hint_key, t.hint_vars || {}) : '';
  if (t.arch_warn) {
    s += tr('flash.hint.archMismatch', { arch: t.arch_warn[0], mach: t.arch_warn[1] });
  }
  return s;
}
function renderFlashTools() {
  const el = $('flashTools');
  // 只显示已找到的工具, 未找到的不展示
  const tools = (state.flashTools || []).filter((t) => t.found);
  if (tools.length === 0) {
    el.innerHTML = `<span class="hint">${tr(isQualcomm() ? 'pjs.noFlashScript' : 'pjs.noFlashTool')}</span>`;
    $('selFlashTool').innerHTML = `<option value="">${tr('pjs.noTool')}</option>`;
    return;
  }
  el.innerHTML = tools.map((t) => `
    <div class="tool-item">
      <span class="dot dot-ok"></span>
      <span class="mono">${esc(t.name)}</span>
      <span class="hint">${esc(flashToolHint(t))}</span>
      <span class="badge badge-on">${tr('pjs.available')}</span>
    </div>`).join('');
  const sel = $('selFlashTool');
  sel.innerHTML = tools.map((t) => `<option value="${esc(t.name)}">${esc(t.name)}</option>`).join('');
}

/* 烧录进度显示 (解析工具输出里的百分比 / 阶段) */
let flashProgressTimer = null;
function setFlashProgress(pct, text, cls = '') {
  const box = $('flashProgress');
  box.classList.remove('hidden', 'done', 'fail', 'running');
  if (cls) { box.classList.add(cls); }
  if (cls !== 'done' && cls !== 'fail') { box.classList.add('running'); }
  $('flashProgressFill').style.width = Math.max(0, Math.min(100, pct)) + '%';
  $('flashProgressText').textContent = text;
  clearTimeout(flashProgressTimer);
  if (cls === 'done' || cls === 'fail') {
    // 完成/失败: 保留 15 秒后隐藏, 方便用户看清结果
    flashProgressTimer = setTimeout(() => box.classList.add('hidden'), 15000);
  }
  // 进行中: 常驻显示, 不自动隐藏
}
function parseFlashProgress(line) {
  const s = String(line);
  const pullStart = s.match(/\[下载\]\s*正在连接编译机并检查镜像:\s*(.+)/);
  if (pullStart) { setFlashProgress(0, tr('pjs.flashConnecting')); return; }
  // 1) ssh 模式: 先从编译机拉取镜像 (PULL_PROGRESS:n)
  const pull = s.match(/PULL_PROGRESS:(\d{1,3})/);
  if (pull) { setFlashProgress(Math.min(parseInt(pull[1], 10), 100), tr('pjs.flashPulling', { pct: pull[1] })); return; }
  if (/PULL_OK|PULL_DONE/.test(s)) { setFlashProgress(0, tr('pjs.flashImageReady')); return; }
  // 2) 工具输出的百分比 (rkdeveloptool 等)
  const m = s.match(/(\d{1,3})\s*%/);
  if (m) { setFlashProgress(Math.min(parseInt(m[1], 10), 100), tr('pjs.flashWritingPct', { pct: m[1] })); return; }
  // 3) upgrade_tool 阶段行 (无百分比, 按阶段给进度文字)
  if (/fail|error|失败|abort/i.test(s) && !/upgrade\s+(firmware|image)\s+(done|success)/i.test(s)) {
    setFlashProgress(0, tr('pjs.flashFail'), 'fail'); return;
  }
  const lo = s.toLowerCase();
  if (/loading firmware/.test(lo)) { setFlashProgress(0, tr('pjs.flashLoadingFw')); return; }
  if (/wait for loader|found loader|maskrom|loader device/.test(lo)) { setFlashProgress(0, tr('pjs.flashWaitLoader')); return; }
  if (/support type|fw ver|loader ver/.test(lo)) { setFlashProgress(0, tr('pjs.flashFwIdentified')); return; }
  if (/start to upgrade/.test(lo)) { setFlashProgress(0, tr('pjs.flashStartUpgrade')); return; }
  const dl = s.match(/download\s+(boot|uboot|misc|recovery|rootfs|oem|userdata|system|kernel|resource|dtb)(\s+start)?/i);
  if (dl) { setFlashProgress(0, tr('pjs.flashWritingPart', { part: dl[1] })); return; }
  if (/download\s+.*\s+start/i.test(s)) { setFlashProgress(0, tr('pjs.flashWritingPartGeneric')); return; }
  if (/upgrade\s+(firmware|image)\s+(done|success)|upgrade\s+success|write\s+ok|烧录成功/i.test(s)) {
    setFlashProgress(100, tr('pjs.flashSuccess'), 'done'); return;
  }
}

function setFlashButtonsDisabled(disabled) {
  $('btnFlash').disabled = disabled;
  document.querySelectorAll('.flash-partition').forEach((button) => { button.disabled = disabled; });
}

function startFlash(method, args, button, label) {
  const qc = isQualcomm();
  const tool = $('selFlashTool').value;
  if (!state.connected) { toast(tr('pjs.needConnShort'), 'error'); return; }
  if (qc && method !== 'flash') {
    // Qualcomm 的 SDK 脚本只做整盘烧录, 不支持单独分区
    toast(tr('msg.h1NoPartitionFlash', { board: (state.platform && state.platform.board) || 'H1' }), 'error');
    return;
  }
  if (!qc && !tool) { toast(tr('pjs.noFlashTool'), 'error'); return; }
  if ($('btnFlash').disabled) { return; }
  setFlashButtonsDisabled(true);
  button.textContent = tr('pjs.flashing');
  setFlashProgress(0, qc ? tr('pjs.flashWaitEdl') : tr('pjs.flashWaitLoader'));
  // Qualcomm: 忽略工具与镜像参数, 后端按平台调用 SDK 自带烧录脚本
  const finalArgs = qc ? ['', ''] : [tool, ...args];
  if (!startTask(method, finalArgs)) { resetFlashBtn(); }
}

$('btnFlash').addEventListener('click', () => {
  const image = $('inFlashImage').value.trim() || 'build/result/update.img';
  startFlash('flash', [image], $('btnFlash'), tr('pjs.flashAll'));
});
document.querySelectorAll('.flash-partition').forEach((button) => {
  button.addEventListener('click', () => {
    const partition = button.dataset.partition;
    const label = button.textContent;
    const image = $('inPartitionImage').value.trim();
    startFlash('flashPartition', [partition, image], button, label);
  });
});

/* 恢复烧录按钮 (烧录任务结束) */
function resetFlashBtn() {
  const btn = $('btnFlash');
  setFlashButtonsDisabled(false);
  btn.textContent = flashBtnLabel();
  document.querySelectorAll('.flash-partition').forEach((button) => {
    button.textContent = tr('pjs.flashBootImage');
  });
}

/* ================= 工程创建页 ================= */
function renderTplFilter() {
  const cats = new Set(['all']);
  state.templates.forEach((t) => cats.add(t.category || t.tag || tr('pjs.other')));
  $('tplFilter').innerHTML = [...cats].map((c) =>
    `<button class="filter-btn ${state.tplFilter === c ? 'active' : ''}" data-cat="${esc(c)}">${esc(c)}</button>`).join('');
  document.querySelectorAll('#tplFilter .filter-btn').forEach((b) => {
    b.addEventListener('click', () => {
      state.tplFilter = b.dataset.cat;
      document.querySelectorAll('#tplFilter .filter-btn').forEach((x) => x.classList.toggle('active', x === b));
      renderTemplates();
    });
  });
}

function renderTemplates() {
  const grid = $('templateGrid');
  const list = state.templates.filter((t) =>
    state.tplFilter === 'all' || (t.category || t.tag) === state.tplFilter);
  if (list.length === 0) {
    grid.innerHTML = `<span class="hint">${tr('pjs.noTemplates')}</span>`;
    return;
  }
  grid.innerHTML = list.map((t) => {
    const stars = '★'.repeat(Math.max(0, Math.min(5, t.difficulty || 1))) + '☆'.repeat(Math.max(0, 5 - (t.difficulty || 1)));
    const periph = (t.peripherals || []).slice(0, 3).map((p) => `<span class="chip">${esc(p)}</span>`).join('');
    return `
    <div class="tpl-card" data-id="${esc(t.id)}">
      <div class="tpl-ico">${t.icon || '📄'}</div>
      <div class="tpl-title"><h3>${esc(t.name)}</h3><span class="tag">${esc(t.tag || '')}</span></div>
      <div class="desc">${esc(t.description || '')}</div>
      <div class="tpl-meta"><span class="stars">${stars}</span>${periph}</div>
    </div>`;
  }).join('');
  document.querySelectorAll('#templateGrid .tpl-card').forEach((card) => {
    card.addEventListener('click', () => selectTemplate(card.dataset.id));
  });
}

function selectTemplate(id) {
  const t = state.templates.find((x) => x.id === id);
  if (!t) { return; }
  state.selectedTemplate = t;
  document.querySelectorAll('#templateGrid .tpl-card').forEach((c) => c.classList.toggle('selected', c.dataset.id === id));
  const panel = $('createFormPanel');
  panel.classList.remove('hidden');
  $('createFormTitle').textContent = `${t.icon} ${t.name} — ${tr('panel.createFormTitle')}`;
  const vars = t.variables || {};
  let html = '';
  for (const [key, v] of Object.entries(vars)) {
    const def = v.default ?? '';
    if (v.type === 'choice' && v.choices) {
      html += `
      <div class="row">
        <label class="w-24">${esc(v.label)}</label>
        <select class="grow" data-var="${esc(key)}">
          ${v.choices.map((c) => `<option value="${esc(c.value)}" ${String(c.value) === String(def) ? 'selected' : ''}>${esc(c.label)}</option>`).join('')}
        </select>
      </div>`;
    } else {
      html += `
      <div class="row">
        <label class="w-24">${esc(v.label)}</label>
        <input class="grow" data-var="${esc(key)}" value="${esc(String(def))}" placeholder="${esc(v.hint || '')}" spellcheck="false">
      </div>`;
    }
  }
  html += `
  <div class="row">
    <label class="w-24">${tr('pjs.projName')}</label>
    <input class="grow mono" id="inProjectName" value="${esc(t.id)}_demo" placeholder="${tr('pjs.projNamePh')}" spellcheck="false">
  </div>`;
  $('createForm').innerHTML = html;
  $('createResult').textContent = '';
}

$('btnCreateProject').addEventListener('click', () => {
  const t = state.selectedTemplate;
  if (!t) { toast(tr('pjs.errSelectTpl'), 'error'); return; }
  const name = ($('inProjectName')?.value || '').trim();
  if (!name) { toast(tr('pjs.errProjName'), 'error'); return; }
  if (!/^[A-Za-z0-9_\-]{1,64}$/.test(name)) {
    toast(tr('pjs.errProjNameInvalid'), 'error'); return;
  }
  const variables = {};
  document.querySelectorAll('#createForm [data-var]').forEach((el) => {
    variables[el.dataset.var] = el.value;
  });
  post({ type: 'project-create', name, template: t.id, variables });
});

function renderProjects() {
  const el = $('projectList');
  if (state.projects.length === 0) {
    el.innerHTML = `<span class="hint">${tr('pjs.noProjectsYet')}</span>`;
    return;
  }
  el.innerHTML = state.projects.map((p) => `
    <div class="project-item">
      <div class="proj-info">
        <span>📁</span>
        <div>
          <div class="proj-name">${esc(p.name)} ${p.hasBinary ? `<span class="badge badge-on">${tr('pjs.compiledBadge')}</span>` : ''}</div>
          <div class="hint mono">${esc(p.dir)}</div>
        </div>
      </div>
      <div class="proj-actions">
        <button class="btn btn-sm" data-act="open" data-name="${esc(p.name)}">${tr('pjs.open')}</button>
        <button class="btn btn-sm" data-act="build" data-name="${esc(p.name)}">${tr('pjs.build')}</button>
        <button class="btn btn-sm btn-danger" data-act="del" data-name="${esc(p.name)}">${tr('pjs.delete')}</button>
      </div>
    </div>`).join('');
  el.onclick = (event) => {
    const btn = event.target.closest('button[data-act]');
    if (!btn) { return; }
    const act = btn.dataset.act;
    const name = btn.dataset.name;
    if (!name) { toast(tr('pjs.errProjName2'), 'error'); return; }
    if (act === 'open') {
      const p = state.projects.find((x) => x.name === name);
      if (!p) { toast(tr('pjs.errProjMissing'), 'error'); return; }
      post({ type: 'open-folder', rel: p.dir, sdkRoot: state.sdkRoot, conn: state.conn });
    } else if (act === 'build') {
      const p = state.projects.find((x) => x.name === name);
      if (p) startTask('build', ['app', p.dir]);
    } else if (act === 'del') {
      btn.disabled = true;
      btn.textContent = tr('pjs.deleting');
      toast(tr('pjs.deletingRemote', { name }));
      post({ type: 'project-delete', name });
    }
  };
}
function refreshProjects() { post({ type: 'projects' }); }
$('btnRefreshProjects').addEventListener('click', refreshProjects);

/* ================= 设备页 ================= */
/* adb 原生子命令 (这些不走设备 shell, 不加 shell 前缀) */
const ADB_NATIVE_CMDS = new Set([
  'devices', 'connect', 'disconnect', 'pair', 'push', 'pull', 'sync', 'install',
  'install-multiple', 'uninstall', 'logcat', 'forward', 'reverse', 'reboot',
  'get-state', 'get-serialno', 'get-devpath', 'wait-for-device', 'wait-for-usb',
  'wait-for-local', 'shell', 'emu', 'bugreport', 'backup', 'restore', 'help',
  'version', 'start-server', 'kill-server', 'root', 'unroot', 'usb', 'tcpip',
  'mdns', 'reconnect', 'sideload', 'disable-verity', 'enable-verity', 'keygen',
  'jdwp', 'track-jdwp', 'remount',
]);

/* 自动补 adb shell: 直接输入 ls / cat / dmesg 等设备命令即可执行 */
function buildAdbArgs(raw) {
  let args = String(raw || '').trim();
  if (!args) { return ''; }
  args = args.replace(/^adb\s+/i, ''); // 兼容历史输入 "adb shell xxx"
  const first = args.split(/\s+/)[0] || '';
  if (first !== 'shell' && !ADB_NATIVE_CMDS.has(first)) {
    args = 'shell ' + args;
  }
  return args;
}

$('btnDmesg').addEventListener('click', () => startTask('dmesg', [$('inDmesgFilter').value.trim(), false]));
$('btnDmesgFollow').addEventListener('click', () => startTask('dmesg', [$('inDmesgFilter').value.trim(), true]));
$('btnJctl').addEventListener('click', () => startTask('journalctl', [$('inJctlFilter').value.trim(), false]));
$('btnJctlFollow').addEventListener('click', () => startTask('journalctl', [$('inJctlFilter').value.trim(), true]));
$('btnAdb').addEventListener('click', () => {
  const args = buildAdbArgs($('inAdbCmd').value);
  if (!args) { toast(tr('pjs.errDeviceCmd'), 'error'); return; }
  startTask('adb', [args]);
});

/* ================= AT ================= */
function renderAtPorts(ports) {
  const sel = $('selAtPort');
  const cur = sel.value;
  const list = (ports || []).map((p) => ({
    value: p.device,
    label: p.desc ? `${p.device} (${p.desc})` : p.device,
    kind: p.kind || 'local',
    adb: p.adb_serial || '',
  }));
  if (!list.length) {
    // 兜底: 默认 Linux 端口 (无检测结果时保留)
    ['/dev/ttyACM0', '/dev/ttyACM1', '/dev/ttyUSB0', '/dev/ttyUSB1'].forEach((d) => {
      if (!list.some((x) => x.value === d)) { list.push({ value: d, label: d, kind: 'local', adb: '' }); }
    });
  }
  const groups = { local: [], remote: [], adb: [] };
  list.forEach((p) => {
    if (p.kind === 'adb') { groups.adb.push(p); }
    else if (p.kind === 'remote') { groups.remote.push(p); }
    else { groups.local.push(p); }
  });
  const opt = (p) => `<option value="${esc(p.value)}" data-adb="${esc(p.adb)}" data-kind="${esc(p.kind)}">${esc(p.label)}</option>`;
  let html = '';
  if (groups.local.length) {
    html += `<optgroup label="${tr('pjs.atLocalGroup')}">${groups.local.map(opt).join('')}</optgroup>`;
  }
  if (groups.remote.length) {
    html += `<optgroup label="${tr('pjs.atRemoteGroup')}">${groups.remote.map(opt).join('')}</optgroup>`;
  }
  if (groups.adb.length) {
    html += `<optgroup label="${tr('pjs.atAdbGroup')}">${groups.adb.map(opt).join('')}</optgroup>`;
  }
  sel.innerHTML = html;
  if (cur && list.some((x) => x.value === cur)) { sel.value = cur; }
  updateAtPortHint();
}
function updateAtPortHint() {
  const sel = $('selAtPort');
  const opt = sel.selectedOptions && sel.selectedOptions[0];
  const isAdb = !!(opt && opt.dataset && opt.dataset.adb);
  const kind = (opt && opt.dataset && opt.dataset.kind) || 'local';
  $('atPortHint').textContent = isAdb
    ? tr('pjs.atHintAdb')
    : (kind === 'remote' ? tr('pjs.atHintRemote') : tr('pjs.atHintLocal'));
}
$('btnRefreshPorts').addEventListener('click', () => { post({ type: 'ports' }); });
$('selAtPort').addEventListener('change', updateAtPortHint);

function sendAt() {
  const cmds = $('inAtCmds').value.split('\n').map((s) => s.trim()).filter(Boolean);
  if (!cmds.length) { toast(tr('pjs.errAtCmd'), 'error'); return; }
  const sel = $('selAtPort');
  const opt = sel.selectedOptions && sel.selectedOptions[0];
  const adbSerial = (opt && opt.dataset && opt.dataset.adb) || '';
  const baud = parseInt($('inAtBaud').value, 10) || 115200;
  startTask('at', [cmds, sel.value, adbSerial, baud]);
}
$('btnAtSend').addEventListener('click', sendAt);
document.querySelectorAll('.chip[data-at]').forEach((chip) => {
  chip.addEventListener('click', () => {
    const cur = $('inAtCmds').value.trim();
    $('inAtCmds').value = cur ? cur + '\n' + chip.dataset.at : chip.dataset.at;
  });
});

/* ================= 音频 ================= */
$('btnAudioCollect').addEventListener('click', () => {
  startTask('audioCollect', []);
});
$('btnTinymixSpeaker').addEventListener('click', () => {
  const cmd = `shell if command -v tinymix >/dev/null 2>&1; then ` +
    `tinymix set "AUX_RDAC Switch" "1" && ` +
    `tinymix set "RX_MACRO RX0 MUX" "AIF1_PB" && ` +
    `tinymix set "RX INT2_1 MIX1 INP0" "RX0" && tinymix set "LO Switch" "1"; ` +
    `elif command -v amixer >/dev/null 2>&1; then ` +
    `amixer -c 0 sset Speaker on && amixer -c 0 sset "spk switch" on; ` +
    `else echo "no tinymix/amixer" >&2; exit 1; fi`;
  startTask('adb', [cmd]);
});
$('btnTinymix').addEventListener('click', () => {
  const args = $('inTinymixArgs').value.trim();
  if (!args) { toast(tr('pjs.errTinymixArgs'), 'error'); return; }
  startTask('tinymix', [args]);
});

/* ================= 工具 ================= */
$('btnRawExec').addEventListener('click', () => {
  const cmd = $('inRawCmd').value.trim();
  if (!cmd) { toast(tr('pjs.errCmd'), 'error'); return; }
  startTask('exec', [cmd]);
});

/* ================= SKILL ================= */
$('btnReloadSkills').addEventListener('click', () => {
  if (!state.connected) { return toast(tr('pjs.needConnShort'), 'error'); }
  post({ type: 'skills' });
});
$('skillsList').addEventListener('click', (e) => {
  const item = e.target.closest('.skill-item');
  if (!item) { return; }
  post({ type: 'skill-content', name: item.dataset.name });
});

/* ================= 环境引导 (仓库) ================= */
function renderRepoList() {
  const el = $('repoList');
  const comps = state.components || [];
  if (comps.length === 0) {
    el.innerHTML = `<span class="hint">${tr('pjs.repoNoInfo')}</span>`;
    return;
  }
  const missing = comps.filter((c) => !c.exists);
  el.innerHTML = comps.map((c) => {
    const statusCls = c.exists ? 'badge-on' : 'badge-warn';
    const statusText = c.exists ? tr('pjs.repoExists') : tr('pjs.repoMissing');
    const urlVal = state.repos[c.rel] || c.url || '';
    return `
    <div class="repo-item ${c.exists ? 'ok' : 'missing'}">
      <div class="repo-info">
        <span class="badge ${statusCls}">${statusText}</span>
        <span class="repo-label">${esc(c.label)}</span>
        <span class="hint mono">${esc(c.rel)}</span>
      </div>
      ${c.exists ? '' : `
      <div class="repo-actions">
        <input class="grow mono repo-url" data-rel="${esc(c.rel)}" value="${esc(urlVal)}" placeholder="${tr('pjs.repoUrlPh')}">
        <button class="btn btn-primary btn-sm repo-clone" data-rel="${esc(c.rel)}">${tr('pjs.clone')}</button>
      </div>`}
    </div>`;
  }).join('');

  el.querySelectorAll('.repo-url').forEach((inp) => {
    inp.addEventListener('change', () => {
      state.repos[inp.dataset.rel] = inp.value.trim();
    });
  });
  el.querySelectorAll('.repo-clone').forEach((btn) => {
    btn.addEventListener('click', () => {
      const rel = btn.dataset.rel;
      const url = state.repos[rel] || '';
      if (!url) { toast(tr('pjs.errRepoUrl', { rel }), 'error'); return; }
      startTask('repoClone', [rel, url, false]);
    });
  });
  const coreUrl = state.repos['core'] || '';
  $('inCoreUrl').value = coreUrl;
}
$('btnRepoRescan').addEventListener('click', () => {
  if (!state.connected) { return toast(tr('pjs.needConnShort'), 'error'); }
  post({ type: 'repo-status', repos: state.repos });
});
$('btnSaveRepos').addEventListener('click', () => {
  // 收集输入框的值
  document.querySelectorAll('.repo-url').forEach((inp) => {
    state.repos[inp.dataset.rel] = inp.value.trim();
  });
  state.repos['core'] = $('inCoreUrl').value.trim();
  // 保存到扩展 workspaceState
  post({ type: 'save-repos-persist', repos: state.repos });
  toast(tr('pjs.repoSaved'), 'ok');
});
$('btnCloneCore').addEventListener('click', () => {
  const url = $('inCoreUrl').value.trim();
  const dir = $('inCoreDir').value.trim();
  if (!url || !dir) { toast(tr('pjs.errCoreReq'), 'error'); return; }
  startTask('repoCloneCore', [url, dir]);
});

/* ================= extension 消息 ================= */
window.addEventListener('message', (event) => {
  const msg = event.data;
  switch (msg.type) {
    case 'backend-info': {
      const running = msg.running;
      setBadge('backendBadge', running ? tr('pjs.backend', { port: msg.port }) : tr('panel.backendOff'), running ? 'badge-on' : '');
      break;
    }
    case 'backend-restarted': {
      post({ type: 'backend-info' });
      post({ type: 'status' });
      break;
    }
    case 'switch-tab': {
      const btn = document.querySelector(`.tab[data-tab="${msg.tab}"]`);
      if (btn) { btn.click(); }
      break;
    }
    case 'status': {
      state.connected = msg.status.connected;
      state.conn = msg.status.conn;
      state.sdkRoot = msg.status.conn?.sdk_root || '';
      state.projectDir = msg.status.conn?.project_dir || 'projects';
      state.platform = msg.status.platform || null;
      applyPlatformUi();
      setBadge('connBadge', msg.status.connected ? tr('pjs.connected', { display: msg.status.display }) : tr('panel.connOff'),
        msg.status.connected ? 'badge-on' : '');
      $('btnConnect').classList.toggle('hidden', msg.status.connected);
      $('btnDisconnect').classList.toggle('hidden', !msg.status.connected);
      if (msg.status.connected) {
        post({ type: 'probe' });
        post({ type: 'templates' });
        post({ type: 'projects' });
        post({ type: 'examples' });
        post({ type: 'flash-tools' });
      }
      // 设备列表/串口与 SDK 连接无关, 始终刷新 (检测到设备即可操作)
      post({ type: 'devices' });
      post({ type: 'ports' });
      break;
    }
    case 'connect-result': {
      $('btnConnect').disabled = false;
      $('btnConnect').textContent = tr('btn.connect');
      if (msg.ok) {
        toast(tr('pjs.connectedOk'), 'ok');
        post({ type: 'status' });
      } else {
        toast(tr('pjs.connectFailed'), 'error');
      }
      break;
    }
    case 'disconnect-result': {
      toast(tr('pjs.disconnected'), 'ok');
      post({ type: 'status' });
      break;
    }
    case 'probe': {
      const p = msg.data;
      const probeEl = $('probeInfo');
      probeEl.classList.remove('hidden');
      const comps = p.components || {};
      const missing = Object.entries(comps).filter(([, v]) => !v).map(([k]) => k);
      probeEl.textContent = [
        `SDK: ${p.sdk_root}`,
        p.platform && p.platform.board
          ? tr('pjs.platform', { board: p.platform.board, soc: p.platform.soc || '' })
          : '',
        state.conn?.mode === 'ssh'
          ? (state.conn.device_location === 'remote'
            ? tr('connect.deviceLocRemote')
            : tr('connect.deviceLocLocal'))
          : '',
        tr('pjs.templates', { n: p.template_count }),
        missing.length ? tr('pjs.missingComps', { list: missing.join(', ') }) : tr('pjs.compsOk'),
        p.adb_devices ? tr('pjs.adbInfo', { line: p.adb_devices.split('\n').slice(1).join(' ').trim() || tr('pjs.noDevices') }) : '',
      ].filter(Boolean).join('\n');
      break;
    }
    case 'devices': {
      state.devices = msg.data.devices || [];
      renderAdb();
      break;
    }
    case 'templates': {
      state.templates = msg.data.templates || [];
      renderTplFilter();
      renderTemplates();
      break;
    }
    case 'projects': {
      state.projects = msg.data.projects || [];
      renderProjects();
      renderDeploySelect();
      refreshAppDirs();
      break;
    }
    case 'examples': {
      state.examples = msg.data.examples || [];
      refreshAppDirs();
      break;
    }
    case 'flash-tools': {
      state.flashTools = msg.data.tools || [];
      state.platform = msg.data.platform || state.platform;
      applyPlatformUi();
      renderFlashTools();
      break;
    }
    case 'device-tree': {
      state.deviceTreeSources = msg.data.sources || [];
      const select = $('selDeviceTreeSource');
      select.innerHTML = state.deviceTreeSources.length
        ? state.deviceTreeSources.map((source) =>
          `<option value="${esc(source.id)}">${tr('pjs.sourceKind', { kind: source.kind, label: source.label })}</option>`).join('')
        : `<option value="">${tr('dt.noSources')}</option>`;
      if (state.deviceTreeSources.some((source) => source.id === 'compiled-dtb')) {
        select.value = 'compiled-dtb';
      }
      $('deviceTreePath').textContent = msg.data.path
        ? tr('pjs.deviceTreePath', { path: msg.data.path })
        : tr('pjs.deviceTreeNotFound', { err: msg.data.error ? `：${msg.data.error}` : '' });
      renderDeviceTree();
      break;
    }
    case 'device-tree-status-result': {
      const source = state.deviceTreeSources.find((item) => item.id === msg.source);
      const node = source?.nodes.find((item) => item.name === msg.node);
      if (node) { node.enabled = msg.result.enabled; node.properties = { ...node.properties, status: msg.result.enabled ? '"okay"' : '"disabled"' }; }
      toast(tr('pjs.dtStatusDone', {
        action: msg.result.enabled ? tr('action.enabled') : tr('action.disabled'),
        node: msg.node,
      }), 'ok');
      renderDeviceTree();
      break;
    }
    case 'device-tree-properties-result': {
      $('btnSaveDeviceTreeProperties').disabled = false;
      $('deviceTreeEditor').classList.add('hidden');
      toast(tr('pjs.dtPropsSaved', { node: msg.node || msg.result.node }), 'ok');
      renderDeviceTree();
      break;
    }
    case 'device-tree-source-result': {
      const source = state.deviceTreeSources.find((item) => item.id === msg.result.source);
      if (!source) { toast(tr('pjs.dtLocatedRefresh'), 'warn'); refreshDeviceTree(); break; }
      $('selDeviceTreeSource').value = source.id;
      $('inDeviceTreeFilter').value = msg.result.node;
      renderDeviceTree();
      toast(tr('pjs.dtLocated', { source: source.label, node: msg.result.node }), 'ok');
      break;
    }
    case 'ports': {
      renderAtPorts(msg.data.ports || []);
      break;
    }
    case 'project-create-result': {
      const r = msg.result;
      $('createResult').textContent = r.ok ? tr('pjs.created', { dir: r.dir }) : tr('pjs.createFailed', { err: r.error || tr('pjs.createError', { err: tr('act.unknownError') }) });
      if (r.ok) {
        post({ type: 'projects' });
        toast(tr('pjs.createdOk', { dir: r.dir }), 'ok');
      } else {
        toast(r.error || tr('pjs.createError', { err: tr('act.unknownError') }), 'error');
      }
      break;
    }
    case 'project-delete-result': {
      if (msg.result.ok) {
        toast(tr('pjs.deletedOk'), 'ok');
        post({ type: 'projects' });
      } else {
        toast(msg.result.error || tr('pjs.deleteFailed'), 'error');
      }
      break;
    }
    case 'repo-status': {
      state.components = msg.data.components || [];
      renderRepoList();
      const missing = state.components.filter((c) => !c.exists);
      if (missing.length > 0) {
        toast(tr('pjs.repoMissingCount', { n: missing.length }), 'warn');
      } else {
        toast(tr('pjs.repoComplete'), 'ok');
      }
      break;
    }
    case 'skills': {
      const list = $('skillsList');
      list.innerHTML = '';
      msg.data.skills.forEach((s) => {
        const b = document.createElement('button');
        b.className = 'skill-item';
        b.dataset.name = s.name;
        b.textContent = `📄 ${s.name}`;
        list.appendChild(b);
      });
      break;
    }
    case 'skill-content': {
      const el = $('skillContent');
      el.classList.remove('hidden');
      el.textContent = msg.content;
      break;
    }
    case 'task-started': {
      state.tasks[msg.taskId] = { running: true, log: currentLogId() };
      appendLog(tr('pjs.taskStarted', { label: methodLabel(msg.method), args: JSON.stringify(msg.args ?? []) }), 'warn');
      updateStopBtn();
      toast(tr('pjs.taskStartToast', { label: methodLabel(msg.method) }));
      post({ type: 'stream-task', taskId: msg.taskId, method: msg.method });
      break;
    }
    case 'stream-event': {
      const ev = msg.event;
      if (ev.type === 'done') {
        const t = state.tasks[msg.taskId];
        if (t) {
          t.running = false;
          if (ev.status === 'done') { appendLog(tr('pjs.taskDone', { code: ev.exit_code }), 'ok', t.log); }
          else if (ev.status === 'killed') { appendLog(tr('pjs.taskKilled'), 'warn', t.log); }
          else { appendLog(tr('pjs.taskFailed', { code: ev.exit_code }), 'err', t.log); }
        }
        updateStopBtn();
        if (msg.method === 'flash' || msg.method === 'flashPartition') {
          resetFlashBtn();
          if (ev.status === 'done') { setFlashProgress(100, tr('pjs.flashDone'), 'done'); toast(tr('pjs.flashDone'), 'ok'); }
          else if (ev.status === 'killed') { setFlashProgress(0, tr('pjs.flashTerminated'), 'fail'); toast(tr('pjs.flashTerminated'), 'warn'); }
          else { setFlashProgress(0, tr('pjs.flashFail'), 'fail'); toast(tr('pjs.flashFail'), 'error'); }
        } else {
          const label = methodLabel(msg.method);
          if (ev.status === 'done') { toast(tr('pjs.labelDone', { label }), 'ok'); }
          else if (ev.status === 'killed') { toast(tr('pjs.labelTerminated', { label }), 'warn'); }
          else { toast(tr('pjs.labelFailed', { label }), 'error'); }
        }
      } else if (ev.type === 'out' || ev.type === 'err') {
        const task = state.tasks[msg.taskId];
        // 已点击停止或已结束的任务, 残留输出直接忽略, 不再渲染到界面
        if (task && (task.stopping || !task.running)) { break; }
        if (msg.method === 'flash' || msg.method === 'flashPartition') { parseFlashProgress(ev.line); }
        appendLog(ev.line, ev.type === 'err' ? 'err' : '', task && task.log);
      }
      break;
    }
    case 'stream-error': {
      appendLog(tr('pjs.streamError', { err: msg.error }), 'err');
      const t = state.tasks[msg.taskId];
      if (t) { t.running = false; }
      updateStopBtn();
      if (msg.method === 'flash' || msg.method === 'flashPartition') { resetFlashBtn(); setFlashProgress(0, tr('pjs.flashFail'), 'fail'); }
      toast(tr('pjs.taskError', { err: msg.error }), 'error');
      break;
    }
    case 'task-cancelled': {
      appendLog(tr('pjs.taskCancelled', { label: methodLabel(msg.method) }), 'warn');
      break;
    }
    case 'task-error': {
      if (msg.method === 'flash' || msg.method === 'flashPartition') { resetFlashBtn(); setFlashProgress(0, msg.error, 'fail'); }
      appendLog(`✗ [${methodLabel(msg.method)}] ${msg.error}`, 'err');
      toast(tr('pjs.taskError', { err: msg.error }), 'error');
      break;
    }
    case 'sdk-path-picked': {
      $('inSdkRoot').value = msg.path;
      break;
    }
    case 'open-folder-result': {
      toast(msg.ok ? tr('pjs.openedFolder') : (msg.error || tr('pjs.openFolderFail', { err: tr('act.unknownError') })), msg.ok ? 'ok' : 'error');
      break;
    }
    case 'load-connection': {
      if (msg.conn) { applyConn(msg.conn); }
      break;
    }
    case 'load-repos': {
      if (msg.repos) {
        state.repos = msg.repos;
        renderRepoList();
      }
      break;
    }
    case 'error': {
      $('btnConnect').disabled = false;
      $('btnConnect').textContent = tr('btn.connect');
      if (msg.method === 'flash') { resetFlashBtn(); setFlashProgress(0, msg.error, 'fail'); }
      appendLog(`✗ ${msg.error}`, 'err');
      toast(msg.error, 'error');
      break;
    }
    default:
      break;
  }
});

/* ================= 初始化 ================= */
(function init() {
  localizeStatic();
  initLogWindows();
  post({ type: 'backend-info' });
  post({ type: 'status' });
  post({ type: 'load-connection' });
  post({ type: 'load-repos' });
  setInterval(() => {
    post({ type: 'devices' });
  }, 30000);
})();

/* ================= 平台适配 (H1/Qualcomm vs M2/Rockchip) =================
 * 后端读取 SDK 的 tools/VERSION.txt 得出平台, 这里据此切换烧录页交互:
 * Qualcomm 走 SDK 自带脚本 (无工具/镜像/分区选项), Rockchip 保持原样。
 * 函数声明会提升, 因此放在文件末尾不影响调用点。 */
function isQualcomm() {
  return !!(state.platform && state.platform.family === 'qualcomm');
}
function flashBtnLabel() {
  return isQualcomm() ? tr('pjs.flashAllH1') : tr('pjs.flashAll');
}
function applyPlatformUi() {
  const qc = isQualcomm();
  $('inFlashImage').style.display = qc ? 'none' : '';
  $('selFlashTool').style.display = qc ? 'none' : '';
  $('rowPartitionFlash').style.display = qc ? 'none' : '';
  const hint = $('flashHint');
  hint.dataset.i18n = qc ? 'panel.flashHintH1' : 'panel.flashHint';
  hint.textContent = tr(hint.dataset.i18n);
  $('btnFlash').textContent = flashBtnLabel();
  // 进下载模式按钮: M2 = Loader (adb reboot loader), H1 = EDL 9008 (adb shell reboot edl)
  const rl = $('btnRebootLoader');
  rl.dataset.i18n = qc ? 'panel.rebootEdl' : 'panel.rebootLoader';
  rl.dataset.i18nTitle = qc ? 'panel.rebootEdlTip' : 'panel.rebootLoaderTip';
  rl.textContent = tr(rl.dataset.i18n);
  rl.title = tr(rl.dataset.i18nTitle);
}
