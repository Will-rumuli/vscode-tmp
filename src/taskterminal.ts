import * as vscode from 'vscode';
import { ApiClient, TaskInfo, StreamEvent } from './api';
import { t } from './i18n';

function formatBytes(n: number): string {
  if (n < 1024) { return `${n}B`; }
  const units = ['KB', 'MB', 'GB', 'TB'];
  let v = n;
  let i = -1;
  do { v /= 1024; i++; } while (v >= 1024 && i < units.length - 1);
  return `${v.toFixed(1)}${units[i]}`;
}

class TaskPty implements vscode.Pseudoterminal {
  private readonly writeEmitter = new vscode.EventEmitter<string>();
  private readonly closeEmitter = new vscode.EventEmitter<number>();
  readonly onDidWrite = this.writeEmitter.event;
  readonly onDidClose = this.closeEmitter.event;

  private terminal: vscode.Terminal | null = null;
  private closed = false;
  /** IDE 终端实际列数 (VS Code 在 open/setDimensions 时告知; 0 = 未知)。 */
  private cols = 0;
  /** 上次 raw 透传后光标可能停在本行中间 (进度条原地刷新), 写整行前需先回车换行。 */
  private rawPending = false;

  private statusBarItem: vscode.StatusBarItem | null = null;
  private progressReport: ((v: { message?: string; increment?: number }) => void) | null = null;
  private progressDone: (() => void) | null = null;
  private lastPct = -1;
  private lastLabel = '';
  /** 本次任务输出里是否出现过 sudo 认证失败 (用于清除前端缓存的密码) */
  private sawSudoError = false;

  constructor(
    private readonly title: string,
    private readonly getApi: () => ApiClient,
    private readonly task: TaskInfo,
    /** 任务结束回调: (退出码, 是否出现 sudo 认证失败) */
    private readonly onDone?: (code: number | null, sawSudoError: boolean) => void,
  ) {}

  open(initialDimensions?: vscode.TerminalDimensions): void {
    this.setDimensions(initialDimensions);
    this.writeln(`\x1b[36m[QPi] ${this.title}\x1b[0m`);
    this.writeln(t('task.id', { id: this.task.task_id }));
    this.writeln('─'.repeat(52));
    void this.stream();
  }

  /** 终端尺寸变化 (含首次 open) 时更新列数, 供进度条裁剪防止折行 */
  setDimensions(dimensions?: vscode.TerminalDimensions): void {
    if (dimensions && dimensions.columns > 0) { this.cols = dimensions.columns; }
  }

  /** 用户点击终端垃圾桶: 关闭终端 -> 杀后端任务 */
  close(): void {
    if (this.closed) { return; }
    this.closed = true;
    this.endProgress();
    void (async () => {
      try {
        await this.getApi().killTask(this.task.task_id);
        this.writeln(`\r\n\x1b[33m${t('task.terminated')}\x1b[0m`);
      } catch { /* 后端可能已退出 */ }
      this.closeEmitter.fire(0);
    })();
  }

  private async stream(): Promise<void> {
    const api = this.getApi();
    api.stream(
      this.task.task_id,
      (ev: StreamEvent) => {
        if (this.closed) { return; }
        if (ev.type === 'raw') {
          // 原样透传 (pty 交互式输出): 直接交给终端模拟器处理 \r 原地刷新、光标上移
          // 与擦除, 绝不在此拆行或重拼 —— 自己重拼会让光标列错位, 日志粘在进度条后面。
          this.noteSudoError(ev.data);
          this.write(ev.data);
          this.rawPending = !/[\n\r]$/.test(ev.data);
        } else if (ev.type === 'out') {
          this.noteSudoError(ev.line);
          // bitbake 进度条 (pty 下由 \r+\x1b[K 原地刷新, executor 按 \r 断行拆成多段):
          // 用 \r 覆盖写还原原地刷新, 避免进度历史刷屏; 普通日志行照常换行。
          const plain = ev.line.replace(/\x1b\[[0-9;?]*[A-Za-z]/g, '').replace(/\x1b\[K/g, '');
          if (/\x1b\[K/.test(ev.line) || /% *\|/.test(plain) || /\| *[#=]/.test(plain)) {
            this.write('\r' + this.fitWidth(ev.line, plain.length) + '\x1b[K');
          } else {
            this.writeln(ev.line);
          }
        } else if (ev.type === 'err') {
          this.noteSudoError(ev.line);
          this.ensureLineStart();
          this.write(`\x1b[31m${ev.line}\x1b[0m`);
        } else if (ev.type === 'meta') {
          this.writeln(`\x1b[90m$ ${ev.cmd}\x1b[0m`);
        } else if (ev.type === 'progress') {
          this.updateProgress(ev.total, ev.done, ev.pct, ev.label);
        } else if (ev.type === 'done') {
          this.writeln('─'.repeat(52));
          const code = ev.exit_code;
          if (code === 0) {
            this.writeln(`\x1b[32m${t('task.done', { status: ev.status ? ` · ${ev.status}` : '' })}\x1b[0m`);
          } else if (code === null) {
            this.writeln(`\x1b[33m${t('task.stopped', { status: ev.status || t('task.terminated') })}\x1b[0m`);
          } else {
            this.writeln(`\x1b[31m${t('task.failed', { code, status: ev.status ? ` · ${ev.status}` : '' })}\x1b[0m`);
          }
          this.endProgress();
          this.onDone?.(code, this.sawSudoError);
        }
      },
      (err: string) => {
        if (!this.closed) { this.writeln(`\x1b[31m${t('task.streamError', { err })}\x1b[0m`); }
        this.endProgress();
      },
    ).then(() => {
      this.terminal?.show(false);
    }).catch((e: Error) => {
      if (!this.closed) { this.writeln(`\x1b[31m${t('task.connectionLost', { err: e.message })}\x1b[0m`); }
      this.endProgress();
    });
  }

  /** 记录输出里是否出现 sudo 认证失败, 供调用方据此清除已缓存的密码 */
  private noteSudoError(line: string): void {
    if (isSudoAuthError(line)) { this.sawSudoError = true; }
  }

  private writeln(s: string): void {
    this.ensureLineStart();
    this.write(s + '\r\n');
  }

  /** raw 透传后光标可能停在本行中间 (进度条原地刷新), 此时再写整行前必须先回车
   *  换行, 否则新行会直接粘在进度条后面。 */
  private ensureLineStart(): void {
    if (this.rawPending) {
      this.rawPending = false;
      this.write('\r\n');
    }
  }

  private write(s: string): void {
    if (!this.closed) { this.writeEmitter.fire(s); }
  }

  /** 进度条超宽时按终端列数裁剪 (保留 ANSI 转义序列), 防止折行。
   *
   *  bitbake 的 Bar 组件会撑满 pty 宽度, 一整行恰好 term_width 个字符。IDE 终端
   *  若比 pty 窄, 这行就会折行; 折行后 \r 只能回到"当前折行"的行首而非该逻辑行的
   *  行首, 于是每次重绘都往下留一行, 表现为进度条累积刷屏 / 日志卡住不动。
   *  裁到终端宽度以内即可根治。cols 未知 (VS Code 未告知尺寸) 时不裁剪。 */
  private fitWidth(line: string, visible: number): string {
    const max = this.cols - 1;
    if (this.cols <= 0 || visible <= max) { return line; }
    let out = '';
    let vis = 0;
    for (let i = 0; i < line.length && vis < max;) {
      if (line[i] === '\x1b') {
        const m = /^\x1b\[[0-9;?]*[A-Za-z]/.exec(line.slice(i));
        if (m) { out += m[0]; i += m[0].length; continue; }
      }
      out += line[i];
      i++;
      vis++;
    }
    return out;
  }

  private beginProgress(label: string): void {
    this.statusBarItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 100);
    this.statusBarItem.text = `$(cloud-download) ${label} 0%`;
    this.statusBarItem.show();
    // 通知标题固定为任务名 (withProgress 的 title 中途不可改),
    // 阶段文案与百分比一律走 message, 这样"下载中"->"提取 sysroot"的切换能体现出来
    void vscode.window.withProgress(
      {
        location: vscode.ProgressLocation.Notification,
        title: this.title,
        cancellable: false,
      },
      (progress) => new Promise<void>((resolve) => {
        this.progressReport = (v) => progress.report(v);
        this.progressDone = resolve;
      }),
    );
  }

  /** 进度文案: 脚本给的 label 优先 (脚本最了解自己的阶段), 没有则用默认"下载中" */
  private progressLabel(label?: string): string {
    const s = (label ?? '').trim();
    return s || t('task.downloading');
  }

  private updateProgress(total: number, done: number, pct: number, label?: string): void {
    const text = this.progressLabel(label);
    // 阶段切换 (如 下载 -> 提取 sysroot): 先收尾旧进度条再开新的。
    // 必须重开而不能沿用: VS Code 的 increment 是累加的且到 100% 后钳死,
    // 沿用旧条会让后续阶段变成"文字在动、进度条一直满格"。
    if (this.statusBarItem && text !== this.lastLabel) {
      this.endProgress();
    }
    if (!this.statusBarItem) { this.beginProgress(text); }
    this.lastLabel = text;
    const p = Math.max(0, Math.min(100, pct));
    this.statusBarItem!.text = `$(cloud-download) ${text} ${p}%`;
    const inc = this.lastPct < 0 ? p : Math.max(0, p - this.lastPct);
    this.lastPct = p;
    const bytes = total > 0
      ? `  (${formatBytes(done)} / ${formatBytes(total)})`
      : (done > 0 ? `  (${formatBytes(done)})` : '');
    this.progressReport?.({ message: `${text} ${p}%${bytes}`, increment: inc });
  }

  private endProgress(): void {
    this.progressDone?.();
    this.progressDone = null;
    this.progressReport = null;
    this.statusBarItem?.hide();
    this.statusBarItem?.dispose();
    this.statusBarItem = null;
    this.lastPct = -1;
    this.lastLabel = '';
  }

  setTerminal(t: vscode.Terminal): void {
    this.terminal = t;
  }
}

export class TaskTerminalManager {
  constructor(private readonly getApi: () => ApiClient) {}

  /** 启动任务并打开独立终端。launcher 调用 api 返回 TaskInfo; 返回该任务 (失败时 undefined)。 */
  async run(
    title: string,
    launcher: (api: ApiClient) => Promise<TaskInfo>,
    onDone?: (code: number | null, sawSudoError: boolean) => void,
  ): Promise<TaskInfo | undefined> {
    const api = this.getApi();
    let task: TaskInfo;
    try {
      task = await launcher(api);
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      vscode.window.showErrorMessage(t('task.startFailed', { title, err: msg }));
      return;
    }

    const pty = new TaskPty(title, this.getApi, task, onDone);
    const terminal = vscode.window.createTerminal({
      name: `QPi: ${title}`,
      pty,
      iconPath: new vscode.ThemeIcon('circuit-board'),
    });
    pty.setTerminal(terminal);
    terminal.show(true);
    return task;
  }
}

/**
 * 该行是否为 sudo 认证失败 (密码错 / 无 tty / askpass 取不到密码 / 重试提示)。
 * 定义在本模块而不是 actions.ts: actions.ts 已经 import 本模块, 反向 import 会循环。
 */
export function isSudoAuthError(line: string): boolean {
  return /sudo:.*(incorrect password|a password is required|no tty present|terminal is required|no password was provided)/i.test(line)
    || /Sorry, try again/i.test(line);
}
