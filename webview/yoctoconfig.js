/* Yocto 构建环境配置面板前端: 下拉选择固件类型/项目信息 -> 检查环境 -> 缺依赖时
   填写 sudo 密码自动安装 -> 执行 buildconfig。所有后端调用由宿主完成, 面板只做交互。 */
const vscode = acquireVsCodeApi();
function tr(key, vars) {
  let s = (window.QPI_I18N && window.QPI_I18N[key]) || key;
  if (vars) {
    for (const k in vars) { s = s.split('{' + k + '}').join(String(vars[k])); }
  }
  return s;
}

const errBox = document.getElementById('err');
const stateEl = document.getElementById('state');
const pwdWrap = document.getElementById('pwdWrap');
const sudoPwd = document.getElementById('sudoPwd');
const missingList = document.getElementById('missingList');
const checkBtn = document.getElementById('checkBtn');
const installBtn = document.getElementById('installBtn');
const cancelBtn = document.getElementById('cancelBtn');
const projId = document.getElementById('projId');
const threadsEl = document.getElementById('threads');
const hostInfoEl = document.getElementById('hostInfo');
const threadStateEl = document.getElementById('threadState');

/* 与 src/yoctoThreads.ts 的 YOCTO_THREADS_MAX 保持一致。
   BitBake/make 对线程数没有 "2 的幂" 要求, 这里限上限只为挡住离谱输入。 */
const THREADS_MAX = 256;

/* 读取并校验线程数输入: {value} (0 = 留空 = 沿用 SDK 默认) 或 {error} */
function readThreads() {
  const s = (threadsEl.value || '').trim();
  if (!s) { return { value: 0 }; }
  if (!/^\d+$/.test(s)) { return { error: tr('yocto.threadsBad') }; }
  const n = Number(s);
  if (n < 1 || n > THREADS_MAX) {
    return { error: tr('yocto.threadsRange', { max: THREADS_MAX }) };
  }
  return { value: n };
}

function showErr(msg) { errBox.textContent = msg; errBox.classList.add('show'); }
function clearErr() { errBox.textContent = ''; errBox.classList.remove('show'); }
function setState(text, kind) { stateEl.textContent = text || ''; stateEl.className = kind || ''; }

/* ---- 通用自绘下拉 ---- */
function makeDropdown(btnId, listId, labelId, options, onPick) {
  const btn = document.getElementById(btnId);
  const list = document.getElementById(listId);
  const label = document.getElementById(labelId);
  let cur = null;
  function close() { list.classList.remove('open'); }
  function render() {
    list.innerHTML = '';
    options.forEach((o, i) => {
      const li = document.createElement('li');
      li.textContent = o.label;
      li.title = o.description || '';
      li.addEventListener('click', () => {
        cur = o.value;
        label.textContent = o.label;
        close();
        onPick && onPick(o);
      });
      list.appendChild(li);
    });
  }
  btn.addEventListener('click', (e) => { e.stopPropagation(); list.classList.toggle('open'); });
  document.addEventListener('click', close);
  render();
  return { get: () => cur, set: (v) => {
    const o = options.find((x) => x.value === v);
    if (o) { cur = o.value; label.textContent = o.label; }
  } };
}

/* 新 buildconfig 签名按二维选择: OS (LINUX/DEBIAN/UBUNTU) + 版本 (STD/DBG) */
const osDD = makeDropdown('osBtn', 'osList', 'osLabel', [
  { value: 'LINUX', label: 'LINUX', description: tr('yocto.osLinux') },
  { value: 'UBUNTU', label: 'UBUNTU', description: tr('yocto.osUbuntu') },
  { value: 'DEBIAN', label: 'DEBIAN', description: tr('yocto.osDebian') },
]);
const versionDD = makeDropdown('versionBtn', 'versionList', 'versionLabel', [
  { value: 'STD', label: 'STD', description: tr('yocto.versionStd') },
  { value: 'DBG', label: 'DBG', description: tr('yocto.versionDbg') },
]);
const secDD = makeDropdown('secBtn', 'secList', 'secLabel', [
  { value: 0, label: tr('yocto.no') },
  { value: 1, label: tr('yocto.yes') },
]);
osDD.set('LINUX');
versionDD.set('STD');
secDD.set(0);

function collect() {
  return {
    os: osDD.get(),
    version: versionDD.get(),
    id: projId.value.trim(),
    sec: secDD.get() === 1,
    threads: (threadsEl.value || '').trim(),
  };
}

checkBtn.addEventListener('click', () => {
  clearErr();
  const payload = collect();
  if (!payload.os) { showErr(tr('yocto.errOs')); return; }
  if (!/^[A-Za-z0-9_]{1,64}$/.test(payload.id)) { showErr(tr('yocto.projectIdBad')); return; }
  const th = readThreads();
  if (th.error) {
    showErr(th.error);
    threadsEl.classList.add('invalid');
    threadsEl.focus();
    return;
  }
  threadsEl.classList.remove('invalid');
  checkBtn.disabled = true;
  checkBtn.textContent = tr('yocto.checking');
  vscode.postMessage({ type: 'check', payload });
});

installBtn.addEventListener('click', () => {
  clearErr();
  vscode.postMessage({
    type: 'install',
    payload: {
      password: sudoPwd.value,
      id: projId.value.trim(),
      os: osDD.get(),
      version: versionDD.get(),
      sec: secDD.get() === 1,
      threads: (threadsEl.value || '').trim(),
    },
  });
});

cancelBtn.addEventListener('click', () => vscode.postMessage({ type: 'cancel' }));

threadsEl.addEventListener('input', () => {
  threadsEl.classList.remove('invalid');
  clearErr();
});

projId.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); checkBtn.click(); }
});

/* 告知宿主可以推送初始数据 (早于本消息的 postMessage 会被 webview 丢弃) */
vscode.postMessage({ type: 'ready' });

window.addEventListener('message', (e) => {
  const m = e.data;
  switch (m.type) {
    case 'error':
      showErr(m.payload);
      checkBtn.disabled = false;
      checkBtn.textContent = tr('yocto.checkAndConfig');
      break;
    case 'state': {
      const s = m.payload || {};
      setState(s.text, s.kind);
      // ok/err 都是检查/安装的终态: 恢复按钮 (loading 由点击时设置;
      // 中间态 kind='' 如"正在检查构建环境..."保持加载文案)
      if (s.kind === 'ok' || s.kind === 'err') {
        checkBtn.disabled = false;
        checkBtn.textContent = tr('yocto.checkAndConfig');
      }
      break;
    }
    case 'host': {
      const h = m.payload || {};
      hostInfoEl.innerHTML = '';
      if (h.cpu_count || h.mem_total_mb) {
        const line = document.createElement('div');
        line.textContent = tr('yocto.threadsHost', {
          cpu: h.cpu_count || '?',
          mem: h.mem_total_mb ? Math.round(h.mem_total_mb / 1024) : '?',
          swap: h.swap_total_mb ? (h.swap_total_mb / 1024).toFixed(1) : '0',
        });
        hostInfoEl.appendChild(line);
      }
      if (h.low_memory) {
        const warn = document.createElement('div');
        warn.className = 'warn';
        warn.textContent = tr('yocto.threadsLowMem');
        hostInfoEl.appendChild(warn);
      }
      if (h.suggested_threads && !(threadsEl.value || '').trim()) {
        const sug = document.createElement('div');
        sug.textContent = tr('yocto.threadsSuggest', { n: h.suggested_threads });
        hostInfoEl.appendChild(sug);
        // 仅在该机不满足 SDK 内存要求时预填: 否则沿用 SDK 默认(按核数取,
        // 大内存机器上更快)。用户随时可改。
        if (h.low_memory) { threadsEl.value = String(h.suggested_threads); }
      }
      break;
    }
    case 'threads-saved': {
      const n = (m.payload || {}).n || 0;
      threadStateEl.textContent = n
        ? tr('yocto.threadsSaved', { n })
        : tr('yocto.threadsSavedAuto');
      break;
    }
    case 'needInstall': {
      const p = m.payload || {};
      // 显示缺失清单; 需要 root 时才显示 sudo 密码框
      missingList.innerHTML = '';
      (p.missing || []).forEach((line) => {
        const li = document.createElement('li');
        li.textContent = line;
        missingList.appendChild(li);
      });
      pwdWrap.classList.toggle('hidden', !p.needSudo);
      installBtn.classList.remove('hidden');
      checkBtn.disabled = false;
      checkBtn.textContent = tr('yocto.checkAndConfig');
      installBtn.focus();
      if (p.needSudo) { sudoPwd.focus(); }
      break;
    }
  }
});
