/* 设备树原生侧边栏视图: 勾选框启用/禁用外设, 点击节点编辑顶层属性 / 定位源码。
   替代旧 Webview 的设备树页, 不再弹出独立窗口。 */
import * as vscode from 'vscode';
import { ApiClient, DeviceTreeData, DeviceTreeNode, DeviceTreeSource } from './api';

type NodeKind = 'source' | 'node';

export class DtItem extends vscode.TreeItem {
  constructor(
    public readonly kind: NodeKind,
    label: string,
    public readonly sourceId: string,
    public readonly dtNode?: DeviceTreeNode,
  ) {
    super(label, kind === 'source' ? vscode.TreeItemCollapsibleState.Collapsed : vscode.TreeItemCollapsibleState.None);
    if (kind === 'source') {
      this.iconPath = new vscode.ThemeIcon('file-code');
      this.contextValue = 'dtSource';
    } else {
      this.checkboxState = dtNode?.enabled
        ? vscode.TreeItemCheckboxState.Checked
        : vscode.TreeItemCheckboxState.Unchecked;
      this.iconPath = new vscode.ThemeIcon(dtNode?.enabled ? 'circle-filled' : 'circle-outline');
      this.contextValue = 'dtNode';
      this.tooltip = (dtNode?.path ? dtNode.path + '\n\n' : '') +
        Object.entries(dtNode?.properties ?? {})
          .map(([k, v]) => (v ? `${k} = ${v};` : `${k};`)).join('\n');
      this.description = Object.entries(dtNode?.properties ?? {})
        .filter(([k]) => !['status', 'phandle', 'linux,phandle'].includes(k))
        .slice(0, 2).map(([k, v]) => `${k}=${v}`).join(' · ');
    }
  }
}

export class DeviceTreeProvider implements vscode.TreeDataProvider<DtItem> {
  private readonly emitter = new vscode.EventEmitter<DtItem | undefined>();
  readonly onDidChangeTreeData = this.emitter.event;
  private data: DeviceTreeData | null = null;

  constructor(private readonly getApi: () => ApiClient) {}

  get boardPath(): string {
    return this.data?.path ?? '';
  }

  get loaded(): boolean {
    return this.data !== null;
  }

  async refresh(silent = false): Promise<void> {
    try {
      this.data = await this.getApi().deviceTree();
      this.emitter.fire(undefined);
    } catch (e) {
      if (!silent) {
        vscode.window.showErrorMessage(`QPi 读取设备树失败: ${(e as Error).message}`);
      }
    }
  }

  private preferredSource(): DeviceTreeSource | undefined {
    if (!this.data) { return undefined; }
    return this.data.sources.find((s) => s.id === 'compiled-dtb') ?? this.data.sources[0];
  }

  getTreeItem(el: DtItem): vscode.TreeItem {
    return el;
  }

  getChildren(el?: DtItem): DtItem[] {
    if (!this.data || this.data.sources.length === 0) {
      return [];
    }
    if (!el) {
      const preferred = this.preferredSource();
      return this.data.sources.map((s) => {
        const item = new DtItem('source', `${s.kind}: ${s.label}`, s.id);
        if (s.id === preferred?.id) {
          item.collapsibleState = vscode.TreeItemCollapsibleState.Expanded;
        }
        return item;
      });
    }
    if (el.kind === 'source') {
      const src = this.data.sources.find((s) => s.id === el.sourceId);
      return (src?.nodes ?? []).map((n) => new DtItem('node', n.path || `&${n.name}`, el.sourceId, n));
    }
    return [];
  }

  /** 勾选/取消: 写入可编辑的 DTS/DTSI 源 (DTB 视图下定位到回链的源节点) */
  async toggle(item: DtItem, enabled: boolean): Promise<void> {
    if (item.kind !== 'node' || !item.dtNode) { return; }
    const target = this.resolveEditable(item);
    if (!target) {
      vscode.window.showWarningMessage('QPi: 该节点仅可读 (未找到可回写的 DTS/DTSI 源)');
      return;
    }
    try {
      await this.getApi().setDeviceTreeStatus(target.node.name, enabled, target.source.id);
      item.dtNode.enabled = enabled;
      vscode.window.showInformationMessage(
        `QPi: 已${enabled ? '启用' : '禁用'} ${target.node.name}; 请重新编译 Boot 镜像生效`,
      );
      this.emitter.fire(undefined);
    } catch (e) {
      vscode.window.showErrorMessage(`QPi 设备树保存失败: ${(e as Error).message}`);
      this.emitter.fire(undefined);
    }
  }

  /** 属性编辑: 打开预填的 untitled 文档, 用户改完点"提交"回写 */
  async editProperties(item: DtItem): Promise<void> {
    if (item.kind !== 'node' || !item.dtNode) { return; }
    const target = this.resolveEditable(item);
    if (!target) {
      vscode.window.showWarningMessage('QPi: 该节点仅可读');
      return;
    }
    const text = Object.entries(target.node.properties ?? {})
      .map(([k, v]) => (v ? `${k} = ${v};` : `${k};`)).join('\n');
    const doc = await vscode.workspace.openTextDocument({ content: text, language: 'devicetree' });
    await vscode.window.showTextDocument(doc, { preview: false });
    const ret = await vscode.window.showInformationMessage(
      `正在编辑 &${target.node.name} 的顶层属性, 修改后点击"提交"`, { modal: false }, '提交修改');
    if (ret !== '提交修改') { return; }
    try {
      await this.getApi().setDeviceTreeProperties(target.node.name, target.source.id, doc.getText());
      vscode.window.showInformationMessage(`QPi: 已保存 &${target.node.name} 属性; 请重新编译 Boot 镜像`);
      await this.refresh();
    } catch (e) {
      vscode.window.showErrorMessage(`QPi 属性保存失败: ${(e as Error).message}`);
    }
  }

  /** 定位源码节点 (让后端把节点回写到可编辑源) */
  async locate(item: DtItem): Promise<void> {
    if (item.kind !== 'node' || !item.dtNode) { return; }
    try {
      const r = await this.getApi().deviceTreeSource(item.dtNode.name, item.dtNode.path ?? '');
      vscode.window.showInformationMessage(`QPi: 已定位到 ${r.source} 中的 &${r.node}, 刷新后可编辑`);
      await this.refresh();
    } catch (e) {
      vscode.window.showErrorMessage(`QPi 定位失败: ${(e as Error).message}`);
    }
  }

  /** 找到真正可写的节点: DTB 视图用节点的 source/sourceLabel 回链; 其它源就是自身 */
  private resolveEditable(item: DtItem): { node: DeviceTreeNode; source: DeviceTreeSource } | null {
    if (!item.dtNode || !this.data) { return null; }
    if (item.sourceId === 'compiled-dtb') {
      const srcId = item.dtNode.source;
      const nodeName = item.dtNode.sourceLabel;
      if (!srcId || !nodeName) { return null; }
      const src = this.data.sources.find((s) => s.id === srcId);
      const node = src?.nodes.find((n) => n.name === nodeName);
      if (!src || !node) { return null; }
      return { node, source: src };
    }
    const src = this.data.sources.find((s) => s.id === item.sourceId);
    return src ? { node: item.dtNode, source: src } : null;
  }
}
