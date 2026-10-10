/* 设备信息面板前端逻辑: 分组卡片展示后端返回的结构化设备信息。
   原则: 有则展示、无则隐藏 —— 只渲染实际存在的字段。
   数据通过 host 下发的 'data'/'error' 事件渲染; 刷新/复制以 postMessage 交回宿主。 */
const vscode = acquireVsCodeApi();
function tr(key, vars) {
  let s = (window.QPI_I18N && window.QPI_I18N[key]) || key;
  if (vars) {
    for (const k in vars) { s = s.split('{' + k + '}').join(String(vars[k])); }
  }
  return s;
}
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

const contentEl = document.getElementById('content');
const errorEl = document.getElementById('error');
const devHeadEl = document.getElementById('devHead');
const refreshBtn = document.getElementById('refreshBtn');
const copyBtn = document.getElementById('copyBtn');
const toastEl = document.getElementById('toast');

let LAST_DATA = null;

function toast(msg, kind) {
  toastEl.textContent = msg;
  toastEl.className = 'show ' + (kind || '');
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { toastEl.className = ''; }, 3200);
}

function fmtMB(mb) {
  if (mb == null) { return ''; }
  return mb >= 1024 ? (mb / 1024).toFixed(1) + ' GB' : mb + ' MB';
}

function kv(key, valueHtml) {
  return `<div class="kv"><span class="k">${tr(key)}</span><span class="v">${valueHtml}</span></div>`;
}
function kvRow(label, valueHtml) {
  return `<div class="kv"><span class="k">${esc(label)}</span><span class="v">${valueHtml}</span></div>`;
}

function group(titleKey, icon, body) {
  return `<div class="group"><div class="group-head"><span class="icon">${icon}</span>${tr(titleKey)}</div><div class="group-body">${body}</div></div>`;
}

function pctBar(pct) {
  pct = Math.max(0, Math.min(100, Number(pct) || 0));
  const cls = pct >= 85 ? 'danger' : (pct >= 60 ? 'warn' : '');
  return `<div class="bar-wrap"><div class="bar-track"><div class="bar-fill ${cls}" style="width:${pct}%"></div></div><span class="hint">${pct}%</span></div>`;
}

/* ---- 分组渲染 ---- */
function renderSystem(s) {
  const items = [];
  if (s.model) { items.push(kv('di.f.model', esc(s.model))); }
  if (s.compatible && s.compatible.length) {
    items.push(kv('di.f.compatible', `<div class="chips">${s.compatible.map((c) => `<span class="chip mono">${esc(c)}</span>`).join('')}</div>`));
  }
  if (s.serial) { items.push(kv('di.f.serial', `<span class="mono">${esc(s.serial)}</span>`)); }
  if (s.os) { items.push(kv('di.f.os', esc(s.os))); }
  if (s.kernel) { items.push(kv('di.f.kernel', `<span class="mono">${esc(s.kernel)}</span>`)); }
  if (s.machine) { items.push(kv('di.f.machine', esc(s.machine))); }
  if (s.fw && Object.keys(s.fw).length) {
    const chips = Object.entries(s.fw).map(([k, v]) => `<span class="chip">${esc(k)}: ${esc(v)}</span>`).join('');
    items.push(kv('di.f.fw', `<div class="chips">${chips}</div>`));
  }
  if (s.rootDev) { items.push(kv('di.f.rootDev', `<span class="mono">${esc(s.rootDev)}</span>`)); }
  return items.join('');
}

function renderHardware(hw) {
  const items = [];
  if (hw.cpuModel) { items.push(kv('di.f.cpuModel', esc(hw.cpuModel))); }
  if (hw.cpuCores != null) { items.push(kv('di.f.cpuCores', esc(hw.cpuCores))); }
  if (hw.load && hw.load.length) { items.push(kv('di.f.load', esc(hw.load.join(' / ')))); }
  if (hw.memTotalMB != null) {
    const used = hw.memUsedMB != null ? hw.memUsedMB : 0;
    const avail = hw.memAvailableMB != null ? hw.memAvailableMB : hw.memTotalMB - used;
    const pct = Math.round((used / hw.memTotalMB) * 100);
    const detail = `${used} MB / ${hw.memTotalMB} MB` + (hw.memAvailableMB != null ? ` (${tr('di.f.memAvail')}: ${avail} MB)` : '');
    items.push(kv('di.f.mem', `${pctBar(pct)}<div class="hint" style="margin-top:4px">${esc(detail)}</div>`));
  }
  return items.join('');
}

function renderStorage(st) {
  const items = [];
  if (st.emmcMB != null) { items.push(kv('di.f.emmc', esc(fmtMB(st.emmcMB)))); }
  if (st.partitions && st.partitions.length) {
    const rows = st.partitions.map((p) => `<tr><td class="mono">${esc(p.name)}</td><td>${esc(fmtMB(p.sizeMB))}</td></tr>`).join('');
    items.push(`<div class="kv"><span class="k">${tr('di.f.partitions')}</span><span class="v">
      <table class="table"><tr><th>${tr('di.f.name')}</th><th>${tr('di.f.size')}</th></tr>${rows}</table>
    </span></div>`);
  }
  if (st.mounts && st.mounts.length) {
    const rows = st.mounts.map((m) =>
      `<tr><td class="mono">${esc(m.fs)}</td><td>${esc(m.size)}</td><td>${esc(m.used)}</td><td>${esc(m.avail)}</td><td style="min-width:120px">${pctBar(m.usePct)}</td><td class="mono">${esc(m.mount)}</td></tr>`).join('');
    items.push(`<div class="kv" style="grid-template-columns:minmax(0,1fr)"><span class="v">
      <table class="table"><tr><th>${tr('di.f.fs')}</th><th>${tr('di.f.size')}</th><th>${tr('di.f.used')}</th><th>${tr('di.f.avail')}</th><th>${tr('di.f.usePct')}</th><th>${tr('di.f.mount')}</th></tr>${rows}</table>
    </span></div>`);
  }
  return items.join('');
}

function renderNetwork(net) {
  const items = [];
  if (net.hostname) { items.push(kv('di.f.hostname', esc(net.hostname))); }
  if (net.interfaces && net.interfaces.length) {
    const rows = net.interfaces.map((i) => {
      const ipv4 = (i.ipv4 || []).map((ip) => `<span class="mono">${esc(ip)}</span>`).join('<br>');
      return `<tr><td class="mono">${esc(i.name)}</td><td>${ipv4 || '<span class="hint">-</span>'}</td><td class="mono">${i.mac ? esc(i.mac) : '<span class="hint">-</span>'}</td></tr>`;
    }).join('');
    items.push(`<div class="kv" style="grid-template-columns:minmax(0,1fr)"><span class="v">
      <table class="table"><tr><th>${tr('di.f.name')}</th><th>IP</th><th>MAC</th></tr>${rows}</table>
    </span></div>`);
  }
  return items.join('');
}

function renderThermal(thermal) {
  const chips = thermal.map((z) => {
    if (z.noData) {
      return `<span class="temp nodata">${esc(z.name)}: ${tr('di.noData')}</span>`;
    }
    const cls = z.tempC >= 75 ? 'danger' : (z.tempC >= 60 ? 'warn' : 'good');
    return `<span class="temp ${cls}">${esc(z.name)}: ${esc(z.tempC.toFixed(1))}°C</span>`;
  }).join('');
  return `<div class="chips">${chips}</div>`;
}

/* 电池监控: vbat / ibat-lvl* / bcl-lvl* 也是 thermal zone, 但原始值不是温度 */
function renderThermalPower(power) {
  const chips = power.map((p) => {
    const val = p.kind === 'voltage' ? Number(p.value).toFixed(3) : esc(p.value);
    const unit = p.unit || (p.kind === 'level' ? tr('di.levelUnit') : '');
    return `<span class="temp">${esc(p.name)}: ${val}${unit ? ' ' + esc(unit) : ''}</span>`;
  }).join('');
  return `<div class="chips">${chips}</div><div class="hint" style="margin-top:6px">${tr('di.powerHint')}</div>`;
}

/* 运行时长: 只显示时长本身, 最多两级 (1 天 2 小时 / 2 小时 3 分钟 / 3 分钟) */
function fmtDur(sec) {
  const d = Math.floor(sec / 86400);
  const h = Math.floor((sec % 86400) / 3600);
  const m = Math.floor((sec % 3600) / 60);
  if (d) { return tr('di.dur.d', { n: d }) + ' ' + tr('di.dur.h', { n: h }); }
  if (h) { return tr('di.dur.h', { n: h }) + ' ' + tr('di.dur.m', { n: m }); }
  if (m) { return tr('di.dur.m', { n: m }); }
  return tr('di.dur.s', { n: sec });
}

function renderRuntime(r) {
  const items = [];
  if (r.uptimeSec != null) { items.push(kv('di.f.uptime', esc(fmtDur(r.uptimeSec)))); }
  if (r.procs != null) { items.push(kv('di.f.procs', esc(r.procs))); }
  return items.join('');
}

function render(data) {
  const parts = [];
  if (data.system) { parts.push(group('di.group.system', '🖥️', renderSystem(data.system))); }
  if (data.hardware) { parts.push(group('di.group.hardware', '⚙️', renderHardware(data.hardware))); }
  if (data.storage) { parts.push(group('di.group.storage', '💾', renderStorage(data.storage))); }
  if (data.network) { parts.push(group('di.group.network', '🌐', renderNetwork(data.network))); }
  if (data.thermal && data.thermal.length) { parts.push(group('di.group.thermal', '🌡️', renderThermal(data.thermal))); }
  if (data.thermalPower && data.thermalPower.length) { parts.push(group('di.group.thermalPower', '⚡', renderThermalPower(data.thermalPower))); }
  if (data.runtime) { parts.push(group('di.group.runtime', '⏱️', renderRuntime(data.runtime))); }
  return parts.join('');
}

function renderDevHead(data) {
  const d = data.device || {};
  const serial = d.serial || '';
  const state = d.state || '';
  const ok = String(state).toLowerCase() === 'device';
  devHeadEl.innerHTML = `<span class="dot ${ok ? '' : 'off'}"></span><span>${ok ? tr('di.devOnline') : tr('di.devOffline')}</span>${serial ? ` · <span class="mono">${esc(serial)}</span> [${esc(state)}]` : ''}`;
}

/* ---- 事件 ---- */
refreshBtn.addEventListener('click', () => {
  refreshBtn.disabled = true;
  contentEl.innerHTML = `<span class="hint">${tr('di.loading')}</span>`;
  vscode.postMessage({ type: 'refresh' });
});

copyBtn.addEventListener('click', () => {
  if (!LAST_DATA) { return; }
  const text = JSON.stringify(LAST_DATA, null, 2);
  const done = () => toast(tr('di.copied'), 'ok');
  const fail = () => toast(tr('di.copyFail'), 'err');
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done).catch(() => copyFallback(text) ? done() : fail());
  } else {
    copyFallback(text) ? done() : fail();
  }
});

function copyFallback(text) {
  try {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand('copy');
    document.body.removeChild(ta);
    return ok;
  } catch { return false; }
}

window.addEventListener('message', (e) => {
  const m = e.data;
  if (m.type === 'data') {
    LAST_DATA = m.payload;
    refreshBtn.disabled = false;
    if (!m.payload || !m.payload.ok) {
      errorEl.style.display = 'block';
      errorEl.textContent = (m.payload && m.payload.error) || tr('di.readFail');
      contentEl.innerHTML = '';
      devHeadEl.innerHTML = '';
      return;
    }
    errorEl.style.display = 'none';
    renderDevHead(m.payload);
    contentEl.innerHTML = render(m.payload);
  } else if (m.type === 'error') {
    LAST_DATA = null;
    refreshBtn.disabled = false;
    errorEl.style.display = 'block';
    errorEl.textContent = tr('di.readFail') + ': ' + m.payload;
    contentEl.innerHTML = '';
    devHeadEl.innerHTML = '';
  }
});

// 打开即采集
vscode.postMessage({ type: 'refresh' });
