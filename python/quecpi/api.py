"""FastAPI 路由层: 前端 (VS Code webview) 调用的全部接口。

约定:
- 所有写操作/长任务返回 {task_id}, 前端通过 SSE /api/tasks/{id}/stream 订阅输出
- 探测类接口 (probe/devices/skills/templates) 同步返回 JSON
- 认证: X-API-Key 请求头 (后端启动时生成随机 token)
"""
from __future__ import annotations

import asyncio
import json
import os
import queue
import time
from typing import List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .executor import Executor, Task
from .i18n import L
from .models import ConnectionConfig
from .sdk_ops import SDK

router = APIRouter(prefix="/api")

# 全局单例 (由 server.py 注入)
STATE: dict = {}
API_KEY: str = ""


def set_state(state: dict, api_key: str) -> None:
    global STATE, API_KEY
    STATE = state
    API_KEY = api_key


def get_sdk() -> SDK:
    sdk = STATE.get("sdk")
    if sdk is None:
        raise HTTPException(status_code=400, detail=L("尚未配置连接, 请先 POST /api/connect"))
    return sdk


_device_sdk: Optional[SDK] = None


def get_device_sdk() -> SDK:
    """设备操作 (adb / AT / 日志 / 截图等) 的执行器。

    - 已连接 SDK 时复用连接实例 (本机 adb 操作本身不依赖 sdk_root)
    - 未连接 SDK 时自动创建一个本机 SDK, 使检测到 adb 设备即可操作设备
    """
    sdk = STATE.get("sdk")
    if sdk is not None:
        return sdk
    global _device_sdk
    if _device_sdk is None:
        _device_sdk = SDK(ConnectionConfig(mode="local"))
    return _device_sdk


def get_task_executor() -> Executor:
    """查找任务归属的执行器 (连接 SDK 或设备 SDK)。"""
    sdk = STATE.get("sdk")
    if sdk is not None:
        return sdk.executor
    return get_device_sdk().executor


def invalidate_adb_caches() -> None:
    """清空已解析的 adb 路径缓存 (连接变更/装好 adb 后调用, 使下次重新解析)。"""
    for sdk in (STATE.get("sdk"), _device_sdk):
        if sdk is not None:
            sdk.invalidate_adb_cache()


def check_key(x_api_key: str = Header(default="")) -> None:
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="invalid api key")


# ================= 请求模型 =================
class ConnectReq(BaseModel):
    conn: ConnectionConfig


class ExecReq(BaseModel):
    cmd: str
    cwd: str = ""


class YoctoBuildParams(BaseModel):
    """Yocto SDK 的 buildconfig 参数 (来自「配置构建环境」面板的保存值)。

    编译时会在**同一会话内**先执行 buildconfig 再编译 —— 因为 source build.sh
    会重建 conf/{local,auto}.conf, buildconfig 的追加/sed 必须发生在其之后才有效。
    """
    project_id: str = ""
    os: str = "LINUX"
    version: str = "STD"
    sec: bool = False


class BuildReq(BaseModel):
    mode: str = "all"
    arg: str = ""
    # sudo 密码: 仅 all/rootfs 等需要 root 的编译模式会用到 (前端先经 /sudo/check
    # 确认确实需要才弹窗收集)。经环境变量传给 SDK 的打包脚本, 不进命令行。
    password: str = ""
    # Yocto (BitBake) 并行编译线程数。0 = 不干预 (沿用 SDK 自身设置), >=1 = 固定值。
    # 由后端注入 BB_NUMBER_THREADS / PARALLEL_MAKE 环境变量, 不改 SDK 文件。
    threads: int = 0
    # Yocto SDK: 编译时自动应用的 buildconfig 参数。None = 未配置 (Yocto 编译会明确报错)
    yocto: Optional[YoctoBuildParams] = None


class EnvSetupReq(BaseModel):
    password: str = ""


class DockerSetupReq(BaseModel):
    password: str = ""


class YoctoBuildconfigReq(BaseModel):
    # Yocto SDK 的 buildconfig 参数 (新签名 <proj> <rev> <OS> <VER> [flags]):
    # 项目 ID (rev) / 系统类型 (LINUX|DEBIAN|UBUNTU) / 版本 (STD|DBG) / 附加标志
    project_id: str = ""
    os: str = "LINUX"
    version: str = "STD"
    flags: List[str] = []


class YoctoDeployReq(BaseModel):
    # 烧录编译产物: "boot" -> dtb.bin, "kernel" -> efi.bin, "both" -> 两者 (默认)
    part: str = "both"


class FlashReq(BaseModel):
    tool: str = "upgrade_tool"
    image: str = "build/result/update.img"
    password: str = ""


class FlashPartitionReq(BaseModel):
    tool: str = "upgrade_tool"
    partition: str
    image: str = ""


class DeviceTreeStatusReq(BaseModel):
    node: str
    enabled: bool
    source: str = ""


class DeviceTreePropertiesReq(BaseModel):
    node: str
    source: str
    properties: str


class DeviceTreeSourceReq(BaseModel):
    node: str
    label: str = ""


class DeployReq(BaseModel):
    source: str
    target: str = "/data/local/tmp/"
    kind: str = "auto"
    run: bool = False
    run_cmd: str = ""


class CreateProjectReq(BaseModel):
    name: str
    template: str
    variables: dict = Field(default_factory=dict)


class SdkDownloadReq(BaseModel):
    version: str = Field("", description="SDK 版本 (M1/M2/H1), 小写作子目录名")
    repo: str = Field("", description="git 仓库地址 (与 archive 二选一)")
    branch: str = Field("", description="git 分支名")
    archive: str = Field("", description="归档下载 URL (tar.gz/zip)")
    target_dir: str = Field("", description="保存根目录 (本机绝对路径)")


class AdbReq(BaseModel):
    args: str


class AtReq(BaseModel):
    cmds: List[str]
    port: str = "/dev/ttyACM0"
    adb_serial: str = Field(
        "", description="adb 设备序列号; 非空时通过 adb shell 向端侧设备串口发送 AT")
    baud: int = Field(115200, description="串口波特率")
    bytesize: int = Field(8, description="数据位 5-8 (4 仅 adb 通道)")
    parity: str = Field("N", description="校验位 N/E/O/M/S (无/偶/奇/Mark/Space)")
    stopbits: float = Field(1, description="停止位 1/1.5/2")
    flow: str = Field("none", description="流控 none/sw/hw (无/软 XON-XOFF/硬 RTS-CTS)")


class TinymixReq(BaseModel):
    args: str


class AplayReq(BaseModel):
    file: str = "/tmp/test.wav"


class PlayVideoReq(BaseModel):
    file: str = "/tmp/test.mp4"


class ScreenRecordReq(BaseModel):
    duration: str = "10"


class ScreenshotReq(BaseModel):
    filename: str = ""


class RepoStatusReq(BaseModel):
    repos: dict = Field(default_factory=dict)


class CloneReq(BaseModel):
    rel: str
    url: str
    force: bool = False


class CloneCoreReq(BaseModel):
    url: str
    target_dir: str


# ================= 状态 =================
@router.get("/status")
async def status(_: None = Depends(check_key)):
    sdk = STATE.get("sdk")
    conn = STATE.get("conn")
    # 平台标识 (H1/Qualcomm 或 M2/Rockchip): 前端据此决定烧录/进下载模式的交互
    platform = None
    if sdk is not None:
        try:
            platform = await asyncio.to_thread(sdk.platform)
        except Exception:
            platform = None
    return {
        "ok": True,
        "connected": sdk is not None,
        "conn": conn.dict() if conn else None,
        "display": conn.display() if conn else "",
        "tasks": len(STATE.get("tasks", {})),
        "platform": platform,
    }


@router.post("/connect")
async def connect(req: ConnectReq, _: None = Depends(check_key)):
    """配置并测试连接 (local / ssh)。"""
    conn = req.conn
    if not conn.sdk_root:
        raise HTTPException(status_code=400, detail=L("sdk_root 不能为空"))
    executor = Executor(conn)
    result = await asyncio.to_thread(executor.test_connection)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error") or L("连接失败"))
    sdk = SDK(conn)
    probe = await asyncio.to_thread(sdk.probe)
    # SDK 会创建自己的 Executor，状态和断开逻辑必须保存实际被任务使用的实例。
    STATE["executor"] = sdk.executor
    STATE["sdk"] = sdk
    STATE["conn"] = conn
    # 换了连接 (宿主/设备位置可能变化) -> 丢弃旧的 adb 解析结果
    invalidate_adb_caches()
    return {"ok": True, **result, "probe": probe}


@router.post("/disconnect")
async def disconnect(_: None = Depends(check_key)):
    executor = STATE.get("executor")
    if executor is not None and getattr(executor, "_ssh", None) is not None:
        try:
            executor._ssh.close()
        except Exception:
            pass
    STATE.pop("sdk", None)
    STATE.pop("executor", None)
    STATE.pop("conn", None)
    return {"ok": True}


# ================= 探测 =================
@router.get("/probe")
async def probe(_: None = Depends(check_key)):
    return get_sdk().probe()


@router.get("/devices")
async def devices(_: None = Depends(check_key)):
    return {"devices": await asyncio.to_thread(get_device_sdk().adb_devices_parsed)}


@router.get("/skills")
async def skills(_: None = Depends(check_key)):
    return {"skills": await asyncio.to_thread(get_sdk().list_skills)}


@router.get("/skill")
async def skill(name: str, _: None = Depends(check_key)):
    return {"name": name,
            "content": await asyncio.to_thread(get_sdk().read_skill, name)}


@router.get("/templates")
async def templates(_: None = Depends(check_key)):
    return {"templates": await asyncio.to_thread(get_sdk().list_templates)}


@router.get("/projects")
async def projects(_: None = Depends(check_key)):
    return {"projects": await asyncio.to_thread(get_sdk().list_projects)}


@router.get("/examples")
async def examples(_: None = Depends(check_key)):
    return {"examples": await asyncio.to_thread(get_sdk().list_examples)}


@router.get("/flash-tools")
async def flash_tools(_: None = Depends(check_key)):
    def _collect() -> dict:
        sdk = get_sdk()
        return {"tools": sdk.flash_tools(), "platform": sdk.platform()}
    return await asyncio.to_thread(_collect)


@router.get("/ports")
async def serial_ports(_: None = Depends(check_key)):
    """枚举本机串口 (Windows COM口 / Linux tty), 供 AT 指令端口选择。

    kind 取值: local = 本机 pyserial 端口; windows = WSL 宿主下转发到 Windows 的
    COM 口 (WSL2 无 USB 直通, 真实串口只在 Windows 侧); adb = 端侧设备串口 (走 adb)。
    """
    return {"ports": await asyncio.to_thread(get_device_sdk().list_serial_ports)}


@router.get("/device/info")
async def device_info(_: None = Depends(check_key)):
    """同步采集设备详细信息 (结构化 JSON), 供设备信息面板展示。"""
    try:
        return await asyncio.to_thread(get_device_sdk().device_info)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/result-dir")
async def result_dir(_: None = Depends(check_key)):
    return {"dir": "build/result"}


@router.get("/device-tree")
async def device_tree(_: None = Depends(check_key)):
    return await asyncio.to_thread(get_sdk().device_tree_nodes)


@router.post("/device-tree/status")
async def device_tree_status(req: DeviceTreeStatusReq, _: None = Depends(check_key)):
    try:
        return await asyncio.to_thread(
            get_sdk().set_device_tree_status, req.node, req.enabled, req.source)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/device-tree/properties")
async def device_tree_properties(req: DeviceTreePropertiesReq, _: None = Depends(check_key)):
    try:
        return await asyncio.to_thread(
            get_sdk().set_device_tree_properties, req.node, req.source, req.properties)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/device-tree/source")
async def device_tree_source(req: DeviceTreeSourceReq, _: None = Depends(check_key)):
    try:
        return await asyncio.to_thread(
            get_sdk().resolve_device_tree_source, req.node, req.label)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ================= 任务 =================
def _task_resp(task: Task) -> dict:
    return {"task_id": task.id}


@router.post("/exec")
async def exec_cmd(req: ExecReq, _: None = Depends(check_key)):
    return _task_resp(get_sdk().exec_raw(req.cmd, cwd=req.cwd))


@router.post("/tasks/{task_id}/kill")
async def kill_task(task_id: str, _: None = Depends(check_key)):
    executor = get_task_executor()
    task = executor.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    # taskkill 是同步阻塞调用, 放到线程池执行, 不能卡住事件循环
    # (否则停止按钮和所有其它 API 都会无响应)
    await asyncio.to_thread(task.kill)
    return {"ok": True}


@router.get("/tasks")
async def list_tasks(_: None = Depends(check_key)):
    return {"tasks": get_task_executor().list_tasks()}


@router.get("/tasks/{task_id}/stream")
async def task_stream(task_id: str, _: None = Depends(check_key)):
    """SSE 流式输出。连接后立即发送已缓冲的输出, 然后实时推送。"""
    task = get_task_executor().get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")

    async def gen():
        yield "event: meta\ndata: " + json.dumps(
            {"task_id": task.id, "cmd": task.cmd}) + "\n\n"
        # 先送缓冲中的历史输出
        while True:
            try:
                kind, line = task.output.get_nowait()
                yield f"event: {kind}\ndata: {json.dumps(line)}\n\n"
            except Exception:
                break
        # 实时推送: 必须用 get_nowait + sleep, 绝不能用阻塞的 queue.get(timeout)。
        # 阻塞调用会占死 uvicorn 事件循环: 长任务期间停止按钮、/devices 等
        # 所有 API 全部排队无响应 (表现为"所有指令都卡住")。
        last_keepalive = time.monotonic()
        while task.status == "running":
            try:
                kind, line = task.output.get_nowait()
                yield f"event: {kind}\ndata: {json.dumps(line)}\n\n"
                last_keepalive = time.monotonic()
            except queue.Empty:
                await asyncio.sleep(0.05)
                # 心跳: 长时间无输出时 (如 curl 下载进度用 \r 原地刷新, 不产生换行),
                # 定期发 SSE 注释, 防止前端 fetch (undici) 因 bodyTimeout 掐断连接。
                if time.monotonic() - last_keepalive > 10:
                    yield ": ping\n\n"
                    last_keepalive = time.monotonic()
        # 收尾剩余输出: 任务被停止时不再推送残留缓冲 (已在 kill 时清空, 双保险)
        if task.status != "killed":
            while True:
                try:
                    kind, line = task.output.get_nowait()
                    yield f"event: {kind}\ndata: {json.dumps(line)}\n\n"
                except Exception:
                    break
        yield f"event: done\ndata: {json.dumps({'exit_code': task.exit_code, 'status': task.status})}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


# ================= 工程代码创建 =================
@router.post("/project/create")
async def project_create(req: CreateProjectReq, _: None = Depends(check_key)):
    return await asyncio.to_thread(
        get_sdk().create_project, req.name, req.template, req.variables)


@router.post("/project/delete")
async def project_delete(name: str = Query(...), _: None = Depends(check_key)):
    return await asyncio.to_thread(get_sdk().delete_project, name)


# ================= 编译 =================
@router.post("/build")
async def build(req: BuildReq, _: None = Depends(check_key)):
    try:
        return _task_resp(get_sdk().build(
            mode=req.mode, arg=req.arg, password=req.password,
            threads=req.threads,
            yocto=req.yocto.model_dump() if req.yocto else None))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/sudo/check")
async def sudo_check(mode: str = "all", _: None = Depends(check_key)):
    """探测该编译模式在非交互执行时是否需要 sudo 密码。

    前端据此决定"是否弹窗要密码", 避免每次编译都打扰用户 (免密 sudo 或
    M2 那种不需要 root 的 SDK 都不会弹)。
    """
    return await asyncio.to_thread(get_sdk().check_sudo, mode)


# ================= 构建环境 =================
@router.get("/deps/check")
async def deps_check(_: None = Depends(check_key)):
    """探测缺失的构建依赖: 委托给 SDK 自带 setup-deps.sh (插件不维护依赖清单)。"""
    return await asyncio.to_thread(get_sdk().check_deps)


@router.get("/env/binfmt")
async def env_binfmt_check(_: None = Depends(check_key)):
    """检测 qemu binfmt 注册状态 (跨架构交叉工具链需要)。"""
    return await asyncio.to_thread(get_sdk().check_binfmt)


@router.post("/env/binfmt")
async def env_binfmt_enable(req: EnvSetupReq, _: None = Depends(check_key)):
    """注册 qemu binfmt (sudo update-binfmts --enable)。

    用于 WSL 未启用 systemd 等 binfmt-support.service 不运行的场景。
    密码经进程 stdin 传递。
    """
    try:
        return _task_resp(get_sdk().enable_binfmt(req.password))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/env/setup")
async def env_setup(req: EnvSetupReq, _: None = Depends(check_key)):
    """一键配置构建环境: 调用 SDK 自带 build.sh setenv (依赖补齐/底包下载/校验)。

    sudo 密码经进程 stdin 传递, 仅系统依赖缺失时需要。
    """
    try:
        return _task_resp(get_sdk().setup_env(req.password))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ================= Yocto SDK (buildconfig 配置构建环境) =================
@router.post("/yocto/buildconfig")
async def yocto_buildconfig(req: YoctoBuildconfigReq, _: None = Depends(check_key)):
    """Yocto SDK: 配置构建环境 = buildconfig。

    DEBIAN/UBUNTU 类型会自动下载/校验 rootfs 底包, 下载期间输出
    [QPI-PROGRESS] 标记供前端渲染进度条 (断点续传由 SDK 脚本 curl -C - 处理)。
    """
    try:
        return _task_resp(get_sdk().yocto_buildconfig(
            req.project_id, req.os, req.version, req.flags))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/yocto/env/check")
async def yocto_env_check(_: None = Depends(check_key)):
    """Yocto SDK: 检测构建环境 (apt 系统包 / pyhocon / locale / 磁盘空间)。

    返回缺失清单, 前端据此提示"自动安装缺失依赖" (安装走 /yocto/env/setup)。
    """
    return await asyncio.to_thread(get_sdk().yocto_env_check)


@router.get("/yocto/host")
async def yocto_host_info(_: None = Depends(check_key)):
    """Yocto SDK: 构建机资源 (核数/内存/swap) + 推荐的并行线程数。

    配置面板打开时用它预填线程输入框; SDK 默认按宿主核数取线程, 小内存机器上
    全量编译会被内核 OOM 杀掉 (终端只报一句含糊的 "bitbake-server might have died")。
    """
    return await asyncio.to_thread(get_sdk().yocto_host_info)


@router.post("/yocto/env/setup")
async def yocto_env_setup(req: EnvSetupReq, _: None = Depends(check_key)):
    """Yocto SDK: 自动安装缺失的构建环境依赖 (需 root, 密码经 stdin 传递)。"""
    try:
        return _task_resp(get_sdk().yocto_env_setup(req.password))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/yocto/deploy")
async def yocto_deploy(req: YoctoDeployReq, _: None = Depends(check_key)):
    """Yocto SDK: 烧录编译产物到设备分区 (push dtb.bin / efi.bin, 默认 both)。

    产物缺失时明确提示先编译; 自动探测设备 ESP/DTB 分区设备节点后 dd 写入。
    """
    try:
        return _task_resp(get_sdk().deploy_yocto_boot(req.part))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ================= Docker 环境 (容器内编译) =================
@router.get("/docker/status")
async def docker_status(_: None = Depends(check_key)):
    """探测 SDK 所在机器的 Docker 环境状态 (供 Manage Docker 入口 / 编译预检)。"""
    return await asyncio.to_thread(get_sdk().docker_status)


@router.post("/docker/setup")
async def docker_setup(req: DockerSetupReq, _: None = Depends(check_key)):
    """安装/配置 Docker 并构建 quectel-pi-builder 镜像 (幂等)。

    sudo 密码经环境变量 QPI_SUDO_PASS 注入 sudo 函数, 不进命令行/任务回显。
    """
    try:
        return _task_resp(get_sdk().docker_setup(req.password))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ================= 烧录 =================
@router.post("/flash")
async def flash(req: FlashReq, _: None = Depends(check_key)):
    try:
        return _task_resp(get_sdk().flash_image(tool=req.tool, image=req.image,
                                                password=req.password))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/flash/partition")
async def flash_partition(req: FlashPartitionReq, _: None = Depends(check_key)):
    try:
        return _task_resp(get_sdk().flash_partition(
            tool=req.tool, partition=req.partition, image=req.image))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/deploy")
async def deploy(req: DeployReq, _: None = Depends(check_key)):
    try:
        return _task_resp(get_sdk().deploy(
            source=req.source, target=req.target, kind=req.kind,
            run=req.run, run_cmd=req.run_cmd))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ================= 设备 =================
@router.get("/adb/which")
async def adb_which(refresh: int = 0, _: None = Depends(check_key)):
    """返回本机解析到的 adb 可执行文件路径 (供交互式 adb 终端初始化/展示)。

    refresh=1 时先丢弃缓存重新解析 (装好 adb / 切换宿主后复查)。
    """
    if refresh:
        invalidate_adb_caches()
    return {"path": get_device_sdk()._adb_exe()}


@router.get("/adb/env/check")
async def adb_env_check(_: None = Depends(check_key)):
    """检测本机 adb 环境 (是否可用 / 是否可由插件自动安装 / 是否需 sudo 密码)。"""
    return await asyncio.to_thread(get_device_sdk().adb_env_check)


@router.post("/adb/env/setup")
async def adb_env_setup(req: EnvSetupReq, _: None = Depends(check_key)):
    """本机 (原生 Linux) 自动安装 adb (android-tools-adb; 需 root, 密码经 stdin 传递)。"""
    try:
        return _task_resp(get_device_sdk().adb_setup(req.password))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/adb")
async def adb(req: AdbReq, _: None = Depends(check_key)):
    return _task_resp(get_device_sdk().adb(req.args))


@router.post("/dmesg")
async def dmesg(filter_str: str = "", follow: bool = False,
                _: None = Depends(check_key)):
    return _task_resp(get_device_sdk().dmesg(filter_str=filter_str, follow=follow))


@router.post("/journalctl")
async def journalctl(filter_str: str = "", follow: bool = False,
                     _: None = Depends(check_key)):
    return _task_resp(get_device_sdk().journalctl(filter_str=filter_str, follow=follow))


@router.post("/reboot")
async def reboot(edl: bool = False, _: None = Depends(check_key)):
    return _task_resp(get_device_sdk().reboot(edl=edl))


@router.post("/reboot-loader")
async def reboot_loader(_: None = Depends(check_key)):
    return _task_resp(get_device_sdk().reboot_loader())


# ================= AT =================
@router.post("/at")
async def at(req: AtReq, _: None = Depends(check_key)):
    return _task_resp(get_device_sdk().at(cmds=req.cmds, port=req.port,
                                          adb_serial=req.adb_serial, baud=req.baud,
                                          bytesize=req.bytesize, parity=req.parity,
                                          stopbits=req.stopbits, flow=req.flow))


# ================= 音频 =================
@router.post("/audio/collect")
async def audio_collect(_: None = Depends(check_key)):
    return _task_resp(get_device_sdk().audio_collect_logs())


@router.post("/audio/tinymix")
async def tinymix(req: TinymixReq, _: None = Depends(check_key)):
    return _task_resp(get_device_sdk().tinymix(req.args))


# ================= 媒体播放 (音频/视频) =================
@router.post("/audio/aplay")
async def aplay(req: AplayReq, _: None = Depends(check_key)):
    return _task_resp(get_device_sdk().aplay(file=req.file))


@router.post("/video/play")
async def play_video(req: PlayVideoReq, _: None = Depends(check_key)):
    return _task_resp(get_device_sdk().play_video(file=req.file))


# ================= 工具 =================
@router.post("/screenshot")
async def screenshot(req: ScreenshotReq, _: None = Depends(check_key)):
    return _task_resp(get_device_sdk().screenshot(filename=req.filename))


@router.post("/screen/record")
async def screen_record(req: ScreenRecordReq, _: None = Depends(check_key)):
    try:
        task = get_device_sdk().screen_record(duration=req.duration)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _task_resp(task)


@router.get("/perf/sample")
async def perf_sample(_: None = Depends(check_key)):
    """采集一次性能快照 (CPU/GPU 频率、占用率、温度、帧率), 供性能监控面板轮询。"""
    try:
        return await asyncio.to_thread(get_device_sdk().perf_sample)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ================= 仓库引导 =================
@router.post("/repo/status")
async def repo_status(req: RepoStatusReq, _: None = Depends(check_key)):
    return {"components": await asyncio.to_thread(get_sdk().repo_status, req.repos)}


@router.post("/repo/clone")
async def repo_clone(req: CloneReq, _: None = Depends(check_key)):
    return _task_resp(get_sdk().clone_component(rel=req.rel, url=req.url, force=req.force))


@router.post("/repo/clone-core")
async def repo_clone_core(req: CloneCoreReq, _: None = Depends(check_key)):
    return _task_resp(get_sdk().clone_core(url=req.url, target_dir=req.target_dir))


@router.post("/sdk/download")
async def sdk_download(req: SdkDownloadReq, _: None = Depends(check_key)):
    """下载 SDK 到本机目录 (git clone 或归档), 走本机执行器, 与连接模式无关。"""
    try:
        return _task_resp(get_device_sdk().download_sdk(
            version=req.version, repo=req.repo, branch=req.branch,
            archive=req.archive, target_dir=req.target_dir))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/sdk/git")
async def sdk_git(fetch: int = 1, log_limit: int = 20, _: None = Depends(check_key)):
    """已连接 SDK 目录的 git 状态: 分支 / 落后远端提交数 / 近期提交记录。

    fetch=1 时先联网 git fetch 刷新远端引用 (失败不报错, 返回里 fetch_ok=false),
    供"连接 SDK 后自动检查是否最新"使用。未连接 SDK 时返回 400。
    """
    sdk = get_sdk()
    return await asyncio.to_thread(
        sdk.git_info, bool(fetch), max(1, min(200, log_limit)))
