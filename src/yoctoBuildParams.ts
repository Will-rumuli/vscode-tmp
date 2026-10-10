/* Yocto SDK 构建配置 (「配置构建环境」面板保存，编译时自动应用)。

   设计要点
   --------
   SDK 的 buildconfig 必须与 source **在同一会话内**执行：
   `source build.sh` 内部无参 `. setup-environment`，会把 `conf/local.conf` 与
   `conf/auto.conf` **整份重建**（文件头写着 DO NOT EDIT），而 buildconfig 的
   许多效果是靠 source 之后 append/sed 这两个文件实现的（例如 STD 必需的
   `SKIP_DEPLOY_DEBIAN_GNOME_ROOTFS = "1"`）。SDK README 的交互式流程正是
   `source -> buildconfig -> buildall`，每次增量编译前都要重跑。

   插件的每条命令都是**独立进程**（为了流式输出/可中断/独立 pty），source 的
   效果不跨命令保留。因此这里把面板参数持久化到 workspaceState，编译时随
   请求发给后端，由后端在**同一会话**里复现 `source -> buildconfig -> 编译`，
   行为与手工执行完全一致。
*/
import * as vscode from 'vscode';

export interface YoctoBuildParams {
  /** 项目 ID (buildconfig 的 <rev>) */
  projectId: string;
  /** 系统类型: LINUX | DEBIAN | UBUNTU */
  os: string;
  /** 版本: STD | DBG */
  version: string;
  /** 安全启动 (SEC) */
  sec: boolean;
}

/** workspaceState 键名 */
const KEY = 'qpiSdk.yoctoBuildParams';

const OS_LIST = ['LINUX', 'UBUNTU', 'DEBIAN'];
const VERSION_LIST = ['STD', 'DBG'];

/** 读取已保存的构建配置；未配置或非法返回 undefined */
export function getYoctoBuildParams(
  context: vscode.ExtensionContext,
): YoctoBuildParams | undefined {
  const v = context.workspaceState.get<YoctoBuildParams>(KEY);
  if (!v || typeof v !== 'object') { return undefined; }
  if (!/^[A-Za-z0-9_]{1,64}$/.test(v.projectId || '')) { return undefined; }
  if (!OS_LIST.includes(v.os) || !VERSION_LIST.includes(v.version)) { return undefined; }
  return { projectId: v.projectId, os: v.os, version: v.version, sec: !!v.sec };
}

/** 保存构建配置 */
export async function setYoctoBuildParams(
  context: vscode.ExtensionContext,
  p: YoctoBuildParams,
): Promise<void> {
  await context.workspaceState.update(KEY, {
    projectId: p.projectId,
    os: p.os,
    version: p.version,
    sec: !!p.sec,
  });
}

/** 转成后端 /build 的 `yocto` 字段 (snake_case)；未配置返回 null */
export function yoctoBuildPayload(
  context: vscode.ExtensionContext,
): Record<string, unknown> | null {
  const p = getYoctoBuildParams(context);
  if (!p) { return null; }
  return { project_id: p.projectId, os: p.os, version: p.version, sec: p.sec };
}
