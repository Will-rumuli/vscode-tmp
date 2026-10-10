
const vscode = acquireVsCodeApi();

const localSection = document.getElementById('localSection');
const sshSection = document.getElementById('sshSection');
const pwdField = document.getElementById('pwdField');
const keyField = document.getElementById('keyField');
const errBox = document.getElementById('err');
const connectBtn = document.getElementById('connectBtn');
const cancelBtn = document.getElementById('cancel');
const localSdkRoot = document.getElementById('localSdkRoot');
const host = document.getElementById('host');
const port = document.getElementById('port');
const user = document.getElementById('user');
const password = document.getElementById('password');
const keyPath = document.getElementById('keyPath');
const sdkRoot = document.getElementById('sdkRoot');

function val(el) { return el ? el.value : ''; }
function setVal(el, v) { if (el && v != null) el.value = v; }
function showErr(msg) { errBox.textContent = msg; errBox.classList.add('show'); }
function clearErr() { errBox.textContent = ''; errBox.classList.remove('show'); }

function currentMode() {
  const r = document.querySelector('input[name="mode"]:checked');
  return r ? r.value : 'local';
}
function currentAuth() {
  const r = document.querySelector('input[name="auth"]:checked');
  return r ? r.value : 'password';
}
function currentDeviceLoc() {
  const r = document.querySelector('input[name="deviceLoc"]:checked');
  return r ? r.value : 'local';
}

function refreshVisibility() {
  const mode = currentMode();
  localSection.classList.toggle('hidden', mode !== 'local');
  sshSection.classList.toggle('hidden', mode !== 'ssh');
  const auth = currentAuth();
  pwdField.classList.toggle('hidden', auth !== 'password');
  keyField.classList.toggle('hidden', auth !== 'key');
}

document.querySelectorAll('input[name="mode"]').forEach((r) => r.addEventListener('change', refreshVisibility));
document.querySelectorAll('input[name="auth"]').forEach((r) => r.addEventListener('change', refreshVisibility));

document.getElementById('browseLocal').addEventListener('click', () => {
  vscode.postMessage({ type: 'pickFolder', field: 'localSdkRoot' });
});
document.getElementById('browseKey').addEventListener('click', () => {
  vscode.postMessage({ type: 'pickFolder', field: 'keyPath' });
});

cancelBtn.addEventListener('click', () => vscode.postMessage({ type: 'cancel' }));
connectBtn.addEventListener('click', () => {
  clearErr();
  const payload = {
    mode: currentMode(),
    localSdkRoot: val(localSdkRoot),
    sdkRoot: val(sdkRoot),
    host: val(host),
    port: val(port),
    user: val(user),
    password: val(password),
    keyPath: val(keyPath),
    auth: currentAuth(),
    deviceLoc: currentDeviceLoc(),
  };
  vscode.postMessage({ type: 'submit', payload });
});

// Enter 在输入框里直接提交
[localSdkRoot, host, port, user, password, keyPath, sdkRoot].forEach((el) => {
  if (!el) return;
  el.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); connectBtn.click(); }
  });
});

window.addEventListener('message', (e) => {
  const m = e.data;
  switch (m.type) {
    case 'init': {
      const last = m.payload?.last;
      const d = m.payload?.defaults || {};
      if (last) {
        const isSsh = last.mode === 'ssh';
        document.querySelector(`input[name="mode"][value="${isSsh ? 'ssh' : 'local'}"]`).checked = true;
        if (isSsh) {
          setVal(host, last.host || d.host);
          setVal(port, last.port || d.port);
          setVal(user, last.user || d.user);
          setVal(sdkRoot, last.sdk_root || '');
          const devLoc = last.device_location === 'remote' ? 'remote' : 'local';
          document.querySelector(`input[name="deviceLoc"][value="${devLoc}"]`).checked = true;
          if (last.key_path) {
            document.querySelector('input[name="auth"][value="key"]').checked = true;
            setVal(keyPath, last.key_path);
          } else {
            document.querySelector('input[name="auth"][value="password"]').checked = true;
          }
        } else {
          setVal(localSdkRoot, last.sdk_root || '');
        }
      } else {
        setVal(host, d.host);
        setVal(port, d.port);
        setVal(user, d.user);
        setVal(keyPath, d.keyPath);
      }
      refreshVisibility();
      break;
    }
    case 'error':
      showErr(m.payload || tr('connect.errInvalid'));
      break;
    case 'pickFolderResult': {
      const { field, path } = m.payload || {};
      if (field === 'localSdkRoot') setVal(localSdkRoot, path);
      if (field === 'keyPath') setVal(keyPath, path);
      break;
    }
  }
});
