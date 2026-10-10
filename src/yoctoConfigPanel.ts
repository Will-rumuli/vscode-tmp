/* Yocto SDK 构建环境配置面板: 固件类型/项目 ID/SEC/DEBUG 表单 + Docker 环境检测与配置。
   基于 BasePanel, 交互与「连接 SDK」一致: 面板内下拉选择 + 输入, 点按钮后
   检查 Docker 构建环境, 未就绪时自动安装/构建镜像 (需 sudo 密码), 就绪即保存配置。 */
import * as vscode from 'vscode';
import { ApiClient } from './api';
import { TaskTerminalManager } from './taskterminal';
import { t } from './i18n';
import { BasePanel, escAttr } from './webview/base';
import {
  getYoctoThreads,
  setYoctoThreads,
  parseYoctoThreads,
  YOCTO_THREADS_MAX,
} from './yoctoThreads';
import { setYoctoBuildParams } from './yoctoBuildParams';

const YOCTO_CSS = `
  body{display:flex;justify-content:center;padding:28px 32px;}
  .wrap{max-width:640px;}
  .row{display:flex;gap:12px;}
  .row .field{flex:1;min-width:0;}
  .checkbox-line{display:flex;align-items:center;gap:8px;margin-top:6px;font-size:12px;color:var(--fg);}
  #pwdWrap{border:1px solid var(--error);border-radius:6px;padding:12px 14px;margin-bottom:14px;background:var(--vscode-inputValidation-warningBackground,transparent);}
  #pwdWrap.hidden{display:none;}
  #missingList{margin:6px 0 0;padding-left:18px;color:var(--muted);font-size:12px;line-height:1.7;}
  #state{font-size:12px;margin:4px 0 12px;min-height:18px;}
  #state.ok{color:var(--ok);}
  #state.err{color:var(--error);}
  .actions{display:flex;gap:10px;justify-content:flex-end;margin-top:6px;}
  button.primary:disabled{opacity:.5;cursor:default;}
  #hostInfo{margin-top:6px;}
  #hostInfo .warn{color:var(--error);}
  #threadState{margin-top:4px;color:var(--ok);}
`;

const YOCTO_BODY = (initThreads: number): string => `
<div class="wrap">
  <h1>&#128736; ${t('yocto.panelTitle')}</h1>
  <div class="sub">${t('yocto.panelSub')}</div>

  <div class="errbox" id="err"></div>

  <div class="section">
    <div class="section-title">${t('yocto.sectionProject')}</div>

    <div class="field">
      <label>${t('yocto.pickOs')} <span class="req">*</span></label>
      <div class="dropdown" id="osDropdown">
        <button type="button" class="input dropdown-btn" id="osBtn"><span id="osLabel">${t('yocto.pickOs')}</span><span class="caret">&#9662;</span></button>
        <ul class="dropdown-list" id="osList"></ul>
      </div>
    </div>

    <div class="field">
      <label>${t('yocto.projectId')} <span class="req">*</span></label>
      <input class="input mono" id="projId" value="myproj" placeholder="${escAttr(t('yocto.projectIdPh'))}" />
      <div class="hint">${t('yocto.projectIdHint')}</div>
    </div>

    <div class="row">
      <div class="field">
        <label>${t('yocto.pickVersion')}</label>
        <div class="dropdown" id="versionDropdown">
          <button type="button" class="input dropdown-btn" id="versionBtn"><span id="versionLabel">${t('yocto.versionStd')}</span><span class="caret">&#9662;</span></button>
          <ul class="dropdown-list" id="versionList"></ul>
        </div>
      </div>
      <div class="field">
        <label>${t('yocto.pickSec')}</label>
        <div class="dropdown" id="secDropdown">
          <button type="button" class="input dropdown-btn" id="secBtn"><span id="secLabel">${t('yocto.no')}</span><span class="caret">&#9662;</span></button>
          <ul class="dropdown-list" id="secList"></ul>
        </div>
      </div>
    </div>
  </div>

  <div class="section">
    <div class="section-title">${t('yocto.sectionThreads')}</div>
    <div class="field">
      <label>${t('yocto.threadsLabel')}</label>
      <input class="input mono" id="threads" inputmode="numeric" autocomplete="off"
             value="${initThreads > 0 ? String(initThreads) : ''}"
             placeholder="${escAttr(t('yocto.threadsPh'))}" />
      <div class="hint" id="hostInfo"></div>
      <div class="hint">${t('yocto.threadsHint')}</div>
      <div class="hint" id="threadState"></div>
    </div>
  </div>

  <div id="pwdWrap" class="hidden">
    <div class="field" style="margin-bottom:6px;">
      <label>${t('yocto.sudoForInstall')}</label>
      <input class="input" id="sudoPwd" type="password" placeholder="sudo" />
    </div>
    <div class="hint">${t('yocto.installNote')}</div>
    <ul id="missingList"></ul>
  </div>

  <div id="state"></div>

  <div class="actions">
    <button class="secondary" id="cancelBtn">${t('btn.cancel')}</button>
    <button class="primary" id="checkBtn">${t('yocto.checkAndConfig')}</button>
    <button class="primary hidden" id="installBtn">${t('yocto.installNow')}</button>
  </div>
</div>
`;

export class YoctoConfigPanel extends BasePanel {
  private busy = false;

  static open(
    extensionUri: vscode.Uri,
    getApi: () => ApiClient,
    tasks: TaskTerminalManager,
    context: vscode.ExtensionContext,
  ): YoctoConfigPanel {
    return BasePanel.reuse(
      'qpiSdkYoctoConfig',
      () => new YoctoConfigPanel(extensionUri, getApi, tasks, context),
    );
  }

  private constructor(
    extensionUri: vscode.Uri,
    private readonly getApi: () => ApiClient,
    private readonly tasks: TaskTerminalManager,
    private readonly context: vscode.ExtensionContext,
  ) {
    super({
      viewType: 'qpiSdkYoctoConfig',
      title: () => t('yocto.panelTitle'),
      extensionUri,
      jsFile: 'webview/yoctoconfig.js',
      body: () => YOCTO_BODY(getYoctoThreads(context)),
      style: YOCTO_CSS,
      onMessage: (msg) => void this.onMessage(msg),
    });
  }

  private async onMessage(msg: any): Promise<void> {
    switch (msg.type) {
      case 'ready':
        // webview 就绪后再推宿主资源 (早于 ready 的 postMessage 会被丢弃)
        await this.pushHostInfo();
        break;
      case 'cancel':
        this.dispose();
        break;
      case 'check':
        await this.checkAndConfig(msg.payload);
        break;
      case 'install':
        await this.installAndConfig(msg.payload);
        break;
    }
  }

  /** 把构建机资源与推荐线程数推给前端 (预填输入框 + 低内存提示) */
  private async pushHostInfo(): Promise<void> {
    try {
      this.post('host', await this.getApi().yoctoHost());
    } catch {
      // 探测失败不打扰用户: 输入框留空即为"沿用 SDK 默认"
    }
  }

  /** 第一步: 校验表单 -> 保存构建配置 -> 检查 Docker 构建环境 -> 未就绪时展示配置面板
   *
   *  这里**只保存参数**, 不执行 buildconfig: buildconfig 必须与 source 在同一
   *  会话内 (见 yoctoBuildParams.ts 说明), 因此改由编译流程自己带上参数。 */
  private async checkAndConfig(payload: any): Promise<void> {
    if (this.busy) { return; }
    this.busy = true;
    try {
      const os: string = (payload?.os || '').trim();
      const version: string = (payload?.version || '').trim();
      const id: string = (payload?.id || '').trim();
      if (!os) { this.post('error', t('yocto.errOs')); return; }
      if (!/^[A-Za-z0-9_]{1,64}$/.test(id)) { this.post('error', t('yocto.projectIdBad')); return; }
      // 并行度校验
      const th = parseYoctoThreads(payload?.threads);
      if (th.error) {
        this.post('error', t(th.error, { max: String(YOCTO_THREADS_MAX) }));
        return;
      }
      // 保存配置: 编译时后端会在同一会话内先 buildconfig 再编译
      // (线程数注入 BB_NUMBER_THREADS / PARALLEL_MAKE; 见 yoctoThreads.ts / sdk_ops.py)
      await setYoctoThreads(this.context, th.value ?? 0);
      await setYoctoBuildParams(this.context, {
        projectId: id, os, version, sec: !!payload?.sec,
      });
      this.post('threads-saved', { n: th.value ?? 0 });
      this.post('state', { text: t('yocto.checking'), kind: '' });
      // 构建环境检查 = Docker 环境检查: 编译统一在容器内执行, 宿主无需安装 SDK 依赖。
      const st = await this.getApi().dockerStatus();
      this.post('state', { text: '', kind: '' });
      if (!st.env_dir) {
        this.post('state', { text: t('docker.noEnvDir'), kind: 'err' });
        return;
      }
      if (st.docker_installed && st.daemon_ok && st.image_present) {
        this.post('state', { text: t('yocto.saved'), kind: 'ok' });
        return;
      }
      // Docker 未就绪: 展示缺失说明 + (非 macOS) sudo 密码框
      const missing: string[] = [];
      if (!st.docker_installed) { missing.push(t('yocto.dockerMissing')); }
      else if (!st.daemon_ok) { missing.push(t('yocto.dockerDaemon')); }
      if (st.docker_installed && !st.image_present) {
        missing.push(t('yocto.dockerImage', { image: st.image }));
      }
      let needSudo = false;
      if (st.host_os !== 'darwin') {
        try { needSudo = (await this.getApi().sudoCheck('docker')).needed; }
        catch { needSudo = true; }
      }
      this.post('needInstall', { missing, needSudo });
    } catch (e) {
      this.post('state', { text: t('yocto.envCheckFail', { err: this.errText(e) }), kind: 'err' });
    } finally {
      this.busy = false;
    }
  }

  /** 第二步: 用户确认配置 Docker 环境 (需 sudo 密码) -> 完成后保存配置 */
  private async installAndConfig(payload: any): Promise<void> {
    if (this.busy) { return; }
    this.busy = true;
    try {
      const pwd: string = payload?.password || '';
      // 用户在安装面板停留期间可能又改了线程数: 重新校验并保存
      const th = parseYoctoThreads(payload?.threads);
      if (th.error) {
        this.post('error', t(th.error, { max: String(YOCTO_THREADS_MAX) }));
        return;
      }
      await setYoctoThreads(this.context, th.value ?? 0);
      await setYoctoBuildParams(this.context, {
        projectId: (payload?.id || '').trim(),
        os: (payload?.os || '').trim(),
        version: (payload?.version || '').trim(),
        sec: !!payload?.sec,
      });
      this.post('state', { text: t('yocto.installing'), kind: '' });
      // 配置任务在独立终端运行; 用 onDone 等它结束 (成功才继续)
      let code: number | null = null;
      await new Promise<void>((resolve) => {
        this.tasks.run(
          t('task.dockerSetup'),
          (api) => api.dockerSetup(pwd),
          (c) => { code = c; resolve(); },
        );
      });
      if (code !== 0) {
        this.post('state', { text: t('yocto.installFailed'), kind: 'err' });
        return;
      }
      this.post('threads-saved', { n: th.value ?? 0 });
      this.post('state', { text: t('yocto.saved'), kind: 'ok' });
    } catch (e) {
      this.post('state', { text: this.errText(e), kind: 'err' });
    } finally {
      this.busy = false;
    }
  }

}
