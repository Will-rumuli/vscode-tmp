/* 设备树配置面板前端逻辑: 源文件下拉(自绘) -> 节点列表(启用/禁用 + 属性编辑 + 定位源码)。
   所有数据通过 host 下发的 'data'/'result' 事件渲染; 操作以 vscode.postMessage 交由宿主调用后端。 */
const vscode = acquireVsCodeApi();
function tr(key, vars) {
  let s = (window.QPI_I18N && window.QPI_I18N[key]) || key;
  if (vars) {
    for (const k in vars) { s = s.split('{' + k + '}').join(String(vars[k])); }
  }
  return s;
}

const srcBtn = document.getElementById('srcBtn');
const srcLabel = document.getElementById('srcLabel');
const srcList = document.getElementById('srcList');
const refreshBtn = document.getElementById('refreshBtn');
const filterInput = document.getElementById('filterInput');
const listEl = document.getElementById('nodeList');
const pathEl = document.getElementById('treePath');
const editorEl = document.getElementById('propEditor');
const editorTitle = document.getElementById('propEditorTitle');
const propText = document.getElementById('propText');
const savePropsBtn = document.getElementById('savePropsBtn');
const cancelPropsBtn = document.getElementById('cancelPropsBtn');
const toastEl = document.getElementById('toast');

let SOURCES = [];
let CUR_SRC = -1;
let EDITING = null; // { node, source }

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function toast(msg, kind) {
  toastEl.textContent = msg;
  toastEl.className = 'show ' + (kind || '');
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { toastEl.className = ''; }, 3200);
}

/* ---- 源文件下拉 ---- */
function kindText(s) {
  if (s.kind === 'board-dtsi') { return tr('dt.kind.boardDtsi'); }
  if (s.kind === 'board-dts') { return tr('dt.kind.boardDts'); }
  return s.kind || '';
}
function sourceText(s) {
  // 聚合视图: 用 soc 组装描述 (label 仅为后端回链标识, 不直接展示)
  if (s.id === 'compiled-dtb') { return tr('dt.compiledDtb', { soc: s.soc || 'RK3576' }); }
  return tr('dt.sourceKind', { kind: kindText(s), label: s.label });
}
function renderSources(sources) {
  SOURCES = sources || [];
  srcList.innerHTML = '';
  if (SOURCES.length === 0) {
    CUR_SRC = -1;
    srcLabel.textContent = tr('dt.noSources');
    return;
  }
  SOURCES.forEach((s, i) => {
    const li = document.createElement('li');
    li.textContent = sourceText(s);
    li.addEventListener('click', () => selectSource(i));
    srcList.appendChild(li);
  });
  const preferred = SOURCES.findIndex((s) => s.id === 'compiled-dtb');
  selectSource(preferred >= 0 ? preferred : 0);
}
function selectSource(i) {
  CUR_SRC = i;
  srcLabel.textContent = SOURCES[i] ? sourceText(SOURCES[i]) : '';
  closeList();
  renderNodes();
}
function currentSource() { return (CUR_SRC >= 0 && CUR_SRC < SOURCES.length) ? SOURCES[CUR_SRC] : null; }
function closeList() { srcList.classList.remove('open'); }
function toggleList() { srcList.classList.toggle('open'); }
srcBtn.addEventListener('click', (e) => { e.stopPropagation(); toggleList(); });
document.addEventListener('click', closeList);

/* ---- 节点列表 ---- */
function renderNodes() {
  const source = currentSource();
  if (!source || !source.nodes || source.nodes.length === 0) {
    listEl.innerHTML = `<span class="hint">${tr('dt.noNodes')}</span>`;
    return;
  }
  const filter = filterInput.value.trim().toLowerCase();
  const nodes = source.nodes.filter((node) => !filter ||
    `${node.path || ''} ${node.name} ${JSON.stringify(node.properties || {})}`.toLowerCase().includes(filter));
  if (nodes.length === 0) {
    listEl.innerHTML = `<span class="hint">${tr('dt.noMatches')}</span>`;
    return;
  }
  listEl.innerHTML = nodes.map((node) => {
    const canEdit = source.id !== 'compiled-dtb' || !!node.source;
    const preview = Object.entries(node.properties || {})
      .filter(([k]) => !['status', 'phandle', 'linux,phandle'].includes(k))
      .slice(0, 2).map(([k, v]) => `${k}: ${v}`).join(' · ');
    return `
    <div class="node-item">
      <div class="node-main">
        <span class="node-path mono" title="${esc(node.path || '&' + node.name)}">${esc(node.path || '&' + node.name)}</span>
        <span class="node-props" title="${esc(preview)}">${esc(preview || tr('dt.noProperties'))}</span>
      </div>
      <div class="node-actions">
        <span class="badge ${node.enabled ? 'on' : 'off'}">${node.enabled ? tr('dt.enabled') : tr('dt.disabled')}</span>
        ${canEdit
           ? `<button class="btn sm toggle" data-node="${esc(node.name)}" data-enabled="${node.enabled}">${node.enabled ? tr('dt.disable') : tr('dt.enable')}</button>
             <button class="btn sm edit" data-node="${esc(node.name)}">${tr('dt.properties')}</button>`
           : `<span class="hint">${tr('dt.readOnly')}</span>`}
      </div>
    </div>`;
  }).join('');

  listEl.querySelectorAll('.toggle').forEach((b) => {
    b.addEventListener('click', () => {
      const enabled = b.dataset.enabled !== 'true';
      b.disabled = true; b.textContent = tr('dt.saving');
      vscode.postMessage({ type: 'toggle', node: b.dataset.node, enabled, source: source.id });
    });
  });
  listEl.querySelectorAll('.edit').forEach((b) => {
    b.addEventListener('click', () => {
      const finalNode = source.nodes.find((n) => n.name === b.dataset.node);
      // DTB 是合并视图: 属性回写用自动定位的 DTS/DTSI 原始节点
      const editSource = source.id === 'compiled-dtb' ? (finalNode && finalNode.source) : source.id;
      const editNode = source.id === 'compiled-dtb' ? (finalNode && finalNode.sourceLabel) : finalNode && finalNode.name;
      const node = (SOURCES.find((s) => s.id === editSource) || {}).nodes?.find((n) => n.name === editNode);
      if (!node) { toast(tr('dt.noEditSource'), 'err'); return; }
      EDITING = { node: node.name, source: editSource };
      editorTitle.textContent = tr('dt.editTitle', { node: node.name });
      propText.value = Object.entries(node.properties || {})
        .map(([k, v]) => (v ? `${k} = ${v};` : `${k};`)).join('\n');
      editorEl.classList.remove('hidden');
      propText.focus();
    });
  });
}
refreshBtn.addEventListener('click', () => vscode.postMessage({ type: 'refresh' }));
filterInput.addEventListener('input', renderNodes);

/* ---- 属性编辑 ---- */
savePropsBtn.addEventListener('click', () => {
  if (!EDITING) { return; }
  const btn = savePropsBtn;
  btn.disabled = true; btn.textContent = tr('dt.saving');
  vscode.postMessage({ type: 'saveProperties', node: EDITING.node, source: EDITING.source, properties: propText.value });
});
cancelPropsBtn.addEventListener('click', () => { editorEl.classList.add('hidden'); EDITING = null; });

/* ---- host 事件 ---- */
window.addEventListener('message', (e) => {
  const m = e.data;
  switch (m.type) {
    case 'data': {
      pathEl.textContent = m.payload.path ? tr('dt.treePath', { path: m.payload.path }) : tr('dt.treeNotFound');
      renderSources(m.payload.sources);
      break;
    }
    case 'error':
      pathEl.textContent = tr('dt.readFail');
      listEl.innerHTML = '';
      toast(tr('dt.readFail') + ': ' + m.payload, 'err');
      break;
    case 'status-result': {
      const r = m.payload || {};
      if (r.ok) { toast(tr('dt.statusSaved', { action: r.enabled ? tr('action.enabled') : tr('action.disabled'), node: r.node }), 'ok'); }
      else { toast(tr('dt.saveFailed', { err: r.message }), 'err'); }
      vscode.postMessage({ type: 'refresh' });
      break;
    }
    case 'properties-result': {
      savePropsBtn.disabled = false; savePropsBtn.textContent = tr('dt.saveProps');
      editorEl.classList.add('hidden'); EDITING = null;
      const r = m.payload || {};
      if (r.ok) { toast(tr('dt.propsSaved', { node: r.node }), 'ok'); }
      else { toast(tr('dt.propsSaveFailed', { err: r.message }), 'err'); }
      vscode.postMessage({ type: 'refresh' });
      break;
    }
  }
});

// 打开即读取设备树
vscode.postMessage({ type: 'refresh' });
