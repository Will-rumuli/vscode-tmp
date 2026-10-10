import * as vscode from 'vscode';
import { t } from './i18n';

export class StatusBar implements vscode.Disposable {
  private readonly items: vscode.StatusBarItem[] = [];
  private readonly conn: vscode.StatusBarItem;
  private readonly build: vscode.StatusBarItem;
  private readonly boot: vscode.StatusBarItem;
  private readonly flash: vscode.StatusBarItem;
  private readonly log: vscode.StatusBarItem;

  constructor() {
    const mk = (cmd: string, text: string, tooltip: string, prio: number, prominent = false) => {
      const it = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, prio);
      it.command = cmd;
      it.text = text;
      it.tooltip = tooltip;
      if (prominent) {
        it.backgroundColor = new vscode.ThemeColor('statusBarItem.prominentBackground');
      }
      it.show();
      this.items.push(it);
      return it;
    };

    this.conn = mk('qpiSdk.connect', t('status.disconnected'), t('status.connectTip'), 100, true);
    this.build = mk('qpiSdk.buildApp', t('status.build'), t('status.buildTip'), 99);
    this.boot = mk('qpiSdk.flashBoot', '$(rocket) Boot', t('status.bootTip'), 98);
    this.flash = mk('qpiSdk.flash', t('status.flash'), t('status.flashTip'), 97, true);
    this.log = mk('qpiSdk.dmesg', t('status.logs'), t('status.logsTip'), 96);
  }

  setConnection(connected: boolean, display: string): void {
    this.lastConnected = connected;
    this.lastDisplay = display;
    this.applyConn();
  }

  private lastConnected = false;
  private lastDisplay = '';

  private applyConn(): void {
    if (this.lastConnected) {
      this.conn.text = t('status.connected', { display: this.lastDisplay || t('sidebar.connected') });
      this.conn.tooltip = t('status.connectedTip');
    } else {
      this.conn.text = t('status.disconnected');
      this.conn.tooltip = t('status.connectTip');
    }
  }

  /** 语言切换后重新套用全部状态栏文案 (连接状态沿用最近一次) */
  retranslate(): void {
    this.build.text = t('status.build');
    this.build.tooltip = t('status.buildTip');
    this.boot.tooltip = t('status.bootTip');
    this.flash.text = t('status.flash');
    this.flash.tooltip = t('status.flashTip');
    this.log.text = t('status.logs');
    this.log.tooltip = t('status.logsTip');
    this.applyConn();
  }

  dispose(): void {
    this.items.forEach((i) => i.dispose());
  }
}
