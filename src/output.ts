/* 统一输出通道: 后端启动 / 依赖下载 / 环境配置的实时进度与日志。
   用户在 "输出" 面板 (Ctrl+Shift+U) 选择 "QPi SDK" 即可回看完整过程;
   首次运行需要下载依赖时会自动展开, 让用户看到真实进度而不是只有一个转圈提示。 */
import * as vscode from 'vscode';

let channel: vscode.OutputChannel | null = null;

/** 取输出通道 (懒创建, 全局单例) */
export function out(): vscode.OutputChannel {
  if (!channel) {
    channel = vscode.window.createOutputChannel('QPi SDK');
  }
  return channel;
}

function stamp(): string {
  const d = new Date();
  const p = (n: number): string => String(n).padStart(2, '0');
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

/** 追加一行带时间戳的日志 */
export function log(msg: string): void {
  out().appendLine(`[${stamp()}] ${msg}`);
}

/** 阶段标题 (加分隔线, 便于在长日志里定位) */
export function step(msg: string): void {
  out().appendLine('');
  out().appendLine(`[${stamp()}] ══ ${msg} ══`);
}

/** 展开输出面板 (preserveFocus=true, 不抢用户焦点) */
export function reveal(): void {
  out().show(true);
}

export function disposeOutput(): void {
  channel?.dispose();
  channel = null;
}
