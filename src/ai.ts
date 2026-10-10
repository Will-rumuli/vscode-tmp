/* AI 助手 —— 基于 GitHub Copilot Chat + OpenAI 兼容 Provider (从 uniknect-vscode-extension 迁移)。
   用法:
     openAiAssistant()  确保 Copilot Chat 已安装, 并打开聊天面板
     openOaiConfig()    确保 OpenAI 兼容 Provider 已安装, 并打开其 API Key / Base URL 配置
   无需本插件自带模型/密钥: 对话由 Copilot Chat 承载, 端点与密钥在 OAI Provider 中配置。 */
import * as vscode from 'vscode';
import { t } from './i18n';

/** GitHub Copilot Chat 扩展 (承载聊天面板)。 */
export const COPILOT_CHAT_EXTENSION_ID = 'github.copilot-chat';
/** 语言模型 Provider: 把 OpenAI 兼容端点接入 Copilot Chat。 */
export const OAI_EXTENSION_ID = 'johnny-zhao.oai-compatible-copilot';
/** OAI Provider 贡献的 "打开 API Key / Base URL 编辑器" 命令。 */
export const OAI_OPEN_CONFIG_COMMAND = 'oaicopilot.openConfig';

/** 确保依赖扩展已安装; 缺失或未启用时弹窗引导, 返回是否可用。 */
export async function ensureExtension(extensionId: string, friendlyName: string): Promise<boolean> {
  const ext = vscode.extensions.getExtension(extensionId);
  if (ext) { return true; }
  // 注意: 被禁用的扩展不会出现在扩展宿主注册表中, 这里的 undefined 无法区分
  // "未安装" 与 "已安装但被禁用", 故文案同时覆盖两种情况并引导去扩展面板确认。
  const action = await vscode.window.showErrorMessage(
    t('ai.extMissing', { name: friendlyName }),
    t('ai.extInstall'),
    t('ai.openExtPanel'),
    t('btn.cancel'),
  );
  if (action === t('ai.extInstall')) {
    await vscode.commands.executeCommand('workbench.extensions.installExtension', extensionId);
  } else if (action === t('ai.openExtPanel')) {
    await vscode.commands.executeCommand('workbench.extensions.search', `@id:${extensionId}`);
  }
  return false;
}

/** 打开 AI 助手 (Copilot Chat 面板)。 */
export async function openAiAssistant(): Promise<void> {
  try {
    if (!(await ensureExtension(COPILOT_CHAT_EXTENSION_ID, 'GitHub Copilot Chat'))) { return; }
    await vscode.commands.executeCommand('workbench.action.chat.toggle');
  } catch (err: any) {
    vscode.window.showErrorMessage(t('ai.openFailed', { message: err?.message ?? String(err) }));
  }
}

/** 打开 OAI Provider 配置 (设置 API Key 与 Base URL)。 */
export async function openOaiConfig(): Promise<void> {
  try {
    if (!(await ensureExtension(OAI_EXTENSION_ID, 'OAI Compatible Provider for Copilot'))) { return; }
    await vscode.commands.executeCommand(OAI_OPEN_CONFIG_COMMAND);
  } catch (err: any) {
    vscode.window.showErrorMessage(t('ai.oaiOpenFailed', { message: err?.message ?? String(err) }));
  }
}
