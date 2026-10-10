import * as vscode from 'vscode';
import { t } from './i18n';

export interface ResourceEntry {
  label: string;
  icon: string;
  desc: string;
  action: () => unknown;
}

export class WelcomeProvider implements vscode.TreeDataProvider<WelcomeItem> {
  private readonly _onDidChangeTreeData = new vscode.EventEmitter<WelcomeItem | undefined>();
  readonly onDidChangeTreeData = this._onDidChangeTreeData.event;
  /** createTreeView 返回的句柄: 视图标题默认取自 package.json, 运行时切语言需手动覆盖 */
  view?: vscode.TreeView<WelcomeItem>;

  constructor(private readonly getEntries: () => ResourceEntry[]) {}

  refresh(): void { this._onDidChangeTreeData.fire(undefined); }

  /** 语言切换后: 重设视图标题并重建条目文案 */
  retranslate(): void {
    if (this.view) { this.view.title = t('welcome.viewTitle'); }
    this.refresh();
  }

  getTreeItem(e: WelcomeItem): vscode.TreeItem { return e; }
  getChildren(): WelcomeItem[] {
    return this.getEntries().map((e) => new WelcomeItem(e));
  }
}

class WelcomeItem extends vscode.TreeItem {
  constructor(entry: ResourceEntry) {
    super(entry.label, vscode.TreeItemCollapsibleState.None);
    this.description = entry.desc;
    this.iconPath = new vscode.ThemeIcon(entry.icon);
    this.command = {
      command: 'qpiSdk._resourceAction',
      title: entry.label,
      arguments: [entry],
    };
    this.tooltip = `${entry.label} — ${entry.desc}`;
  }
}

export function registerWelcomeView(
  context: vscode.ExtensionContext,
  deps: {
    docGuide: () => Promise<void>;
    manageSdks: () => Promise<void>;
    openApplication: () => Promise<void>;
    browseSamples: () => unknown;
    openTerminal: () => unknown;
  },
): WelcomeProvider {
  const buildEntries = (): ResourceEntry[] => [
    { label: t('welcome.docGuide'),     icon: 'book',            desc: t('welcome.docGuide.desc'),     action: deps.docGuide },
    { label: t('welcome.manageSdks'),   icon: 'library',        desc: t('welcome.manageSdks.desc'),   action: deps.manageSdks },
    { label: t('welcome.openApp'),      icon: 'folder-opened',  desc: t('welcome.openApp.desc'),      action: deps.openApplication },
    { label: t('welcome.browseSamples'),icon: 'beaker',         desc: t('welcome.browseSamples.desc'),action: deps.browseSamples },
    { label: t('welcome.openTerminal'), icon: 'terminal',       desc: t('welcome.openTerminal.desc'), action: deps.openTerminal },
  ];

  const provider = new WelcomeProvider(buildEntries);

  const treeView = vscode.window.createTreeView('qpiSdk.welcome', { treeDataProvider: provider });
  treeView.title = t('welcome.viewTitle');
  provider.view = treeView;

  context.subscriptions.push(
    treeView,
    vscode.commands.registerCommand('qpiSdk._resourceAction', (entry: ResourceEntry) => entry.action()),
  );
  return provider;
}
