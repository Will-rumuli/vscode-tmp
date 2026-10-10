/* Yocto (BitBake) 并行编译线程数。

   为什么需要它: SDK 默认 `BB_NUMBER_THREADS = 宿主核数`(无上限),
   `PARALLEL_MAKE = "-j 20"`, 全量编译时 BitBake + make 的并发内存开销很大;
   在内存偏小的机器 (尤其 WSL, 默认只给宿主一半内存) 上会触发内核 OOM,
   把 bitbake **server** 进程杀掉。客户端只报一句含糊的
   "bitbake-server might have died or has been forcibly stopped, ie. OOM killed",
   用户往往会误以为是插件或代码编译错误。

   取值语义:
     0 (或未设置) = 不干预, 完全沿用 SDK 自身设置
     >= 1         = 固定线程数 (同时作为 BB_NUMBER_THREADS 与 make -j 的值)
   BitBake/make 都接受任意正整数, 没有 "必须是 2 的幂" 的限制。

   存储: workspaceState —— 与连接配置同款, 面板点「检查并配置」时随
   buildconfig 一起写入, 编译时读取。
*/
import * as vscode from 'vscode';

/** workspaceState 键名 */
const KEY = 'qpiSdk.yoctoThreads';

/** 线程数上限 (防止手输离谱数值把机器拖死) */
export const YOCTO_THREADS_MAX = 256;

/** 读取已保存的线程数; 未配置/非法返回 0 (= 不干预) */
export function getYoctoThreads(context: vscode.ExtensionContext): number {
  const v = context.workspaceState.get<number>(KEY, 0);
  return typeof v === 'number' && Number.isInteger(v) && v > 0 && v <= YOCTO_THREADS_MAX
    ? v
    : 0;
}

/** 保存线程数; <=0 或非法一律存 0 (= 不干预) */
export async function setYoctoThreads(
  context: vscode.ExtensionContext,
  n: number,
): Promise<void> {
  const ok = typeof n === 'number' && Number.isInteger(n) && n > 0 && n <= YOCTO_THREADS_MAX;
  await context.workspaceState.update(KEY, ok ? n : 0);
}

/** 校验用户在面板里的输入。
 *
 *  返回 `{ value }` (value=0 表示留空 = 不干预) 或 `{ error: <i18nKey> }`。
 *  非法的常见原因是 0 / 负数 / 小数 / 非数字。
 */
export function parseYoctoThreads(
  raw: unknown,
): { value?: number; error?: string } {
  const s = String(raw ?? '').trim();
  if (!s) { return { value: 0 }; }              // 留空 = 沿用 SDK 默认
  if (!/^\d+$/.test(s)) { return { error: 'yocto.threadsBad' }; }
  const n = Number(s);
  if (n < 1 || n > YOCTO_THREADS_MAX) { return { error: 'yocto.threadsRange' }; }
  return { value: n };
}
