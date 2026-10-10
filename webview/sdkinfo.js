/* SDK 信息面板前端逻辑: 展示后端 /sdk/git 返回的 SDK 目录 git 状态。
   宿主通过 'data' / 'error' 事件下发; 刷新(联网)与复制以 postMessage 交回宿主。 */
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
const badgeEl = document.getElementById('badge');
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

function kv(label, valueHtml) {
  return `<div class="kv"><span class="k">${esc(label)}</span><span class="v">${valueHtml}</span></div>`;
}

function group(title, icon, body) {
  return `<div class="group"><div class="group-head"><span>${icon}</span>${esc(title)}</div><div class="group-body">${body}</div></div>`;
}

function mono(s) {
  return `<span class="mono">${esc(s)}</span>`;
}

/* ---- 顶部状态徽标: 已是最新 / 落后 N 个版本 / 无法比较 ---- */
function renderBadge(d) {
  if (!d.is_repo || !d.has_upstream) {
    badgeEl.innerHTML = `<span class="badge err">⚠ ${esc(tr('si.notRepo'))}</span>`;
    return;
  }
  if (d.up_to_date) {
    const extra = d.ahead > 0 ? ` · ${esc(tr('si.ahead', { n: d.ahead }))}` : '';
    badgeEl.innerHTML = `<span class="badge ok">✔ ${esc(tr('si.upToDate'))}${extra}</span>`;
  } else {
    badgeEl.innerHTML = `<span class="badge warn">↑ ${esc(tr('si.behind', { n: d.behind }))}</span>`;
  }
}

/* ---- 版本状态 ---- */
function renderStatus(d) {
  const items = [];
  if (d.sdk_root) { items.push(kv(tr('si.f.sdkRoot'), mono(d.sdk_root))); }
  if (d.branch) { items.push(kv(tr('si.f.branch'), mono(d.branch))); }
  if (d.commit) {
    const subj = d.subject ? ` <span class="hint">${esc(d.subject)}</span>` : '';
    items.push(kv(tr('si.f.commit'), mono(d.commit) + subj));
  }
  if (d.author) { items.push(kv(tr('si.f.author'), esc(d.author))); }
  if (d.date) { items.push(kv(tr('si.f.date'), esc(d.date))); }
  if (d.has_upstream) {
    const txt = d.behind > 0 ? tr('si.behind', { n: d.behind }) : tr('si.upToDate');
    const extra = d.ahead > 0 ? ` · ${tr('si.ahead', { n: d.ahead })}` : '';
    items.push(kv(tr('si.group.status'), `<span class="${d.behind > 0 ? '' : 'hint'}">${esc(txt)}${esc(extra)}</span>`));
    if (d.behind > 0) {
      items.push(kv('', `<span class="hint">${esc(tr('si.behindHint', { n: d.behind }))}</span>`));
    }
  }
  let note = '';
  if (d.has_upstream && !d.fetch_ok) {
    note = `<div class="note">⚠ ${esc(tr('si.fetchFailed'))}</div>`;
  }
  return group(tr('si.group.status'), '📌', items.join('') + note);
}

/* ---- 远程仓库 ---- */
function renderRemote(d) {
  const items = [];
  if (d.remote) { items.push(kv(tr('si.f.remote'), mono(d.remote))); }
  if (d.upstream) { items.push(kv(tr('si.f.upstream'), mono(d.upstream))); }
  if (d.latest_remote) {
    const lr = d.latest_remote;
    const subj = lr.subject ? ` <span class="hint">${esc(lr.subject)}</span>` : '';
    items.push(kv(tr('si.f.latest'), mono(lr.hash) + subj));
  }
  items.push(kv(tr('si.f.shallow'), esc(d.shallow ? tr('si.shallowYes') : tr('si.shallowNo'))));
  return group(tr('si.group.remote'), '🌐', items.join(''));
}

/* ---- 提交记录 ---- */
function renderLog(d) {
  const rows = d.log || [];
  if (rows.length === 0) {
    return group(tr('si.group.log'), '🧾', `<span class="hint">${esc(tr('si.noLog'))}</span>`);
  }
  const body = rows.map((c, i) => {
    const cls = i === 0 ? ' class="head-row"' : '';
    const tag = i === 0 ? ` <span class="pill local">HEAD</span>` : '';
    return `<tr${cls}><td>${mono(c.hash)}${tag}</td><td>${esc(c.subject)}</td><td>${esc(c.author)}</td><td>${esc(c.date)}</td></tr>`;
  }).join('');
  const head = `<tr><th>${esc(tr('si.col.hash'))}</th><th>${esc(tr('si.col.subject'))}</th><th>${esc(tr('si.col.author'))}</th><th>${esc(tr('si.col.date'))}</th></tr>`;
  return group(tr('si.group.log'), '🧾', `<table class="table">${head}${body}</table>`);
}

function render(d) {
  const parts = [];
  if (d.is_repo) {
    parts.push(renderStatus(d));
    parts.push(renderRemote(d));
    parts.push(renderLog(d));
  } else {
    parts.push(group(tr('si.group.status'), '⚠', `<span class="hint">${esc(d.error || tr('si.notRepo'))}</span>`));
  }
  return parts.join('');
}

/* ---- 事件 ---- */
refreshBtn.addEventListener('click', () => {
  refreshBtn.disabled = true;
  contentEl.innerHTML = `<span class="hint">${tr('si.loading')}</span>`;
  badgeEl.innerHTML = '';
  vscode.postMessage({ type: 'refresh' });
});

copyBtn.addEventListener('click', () => {
  if (!LAST_DATA) { return; }
  const text = JSON.stringify(LAST_DATA, null, 2);
  const done = () => toast(tr('si.copied'), 'ok');
  const fail = () => toast(tr('si.copyFail'), 'err');
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
    errorEl.style.display = 'none';
    renderBadge(m.payload);
    contentEl.innerHTML = render(m.payload);
  } else if (m.type === 'error') {
    LAST_DATA = null;
    refreshBtn.disabled = false;
    badgeEl.innerHTML = '';
    errorEl.style.display = 'block';
    errorEl.textContent = tr('si.readFail') + ': ' + m.payload;
    contentEl.innerHTML = '';
  }
});

// 打开即刷新 (联网 fetch 一次, 保证落后版本数是当前值)
vscode.postMessage({ type: 'refresh' });
