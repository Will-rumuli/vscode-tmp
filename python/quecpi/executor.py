"""命令执行器: 本地 subprocess + SSH paramiko 统一封装。

- 任务模型: 每个 exec 调用生成一个 Task, 输出按行进入队列
- 前端通过 SSE 订阅 /api/tasks/{id}/stream 获取流式输出
- 本地与远程对上层暴露相同接口, 上层无需感知连接模式
"""
from __future__ import annotations

import codecs
import os
import queue
import re
import shlex
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from .models import ConnectionConfig
from .i18n import L
from .ssh_client import SSHClient


_BASH_CACHE: Optional[str] = None


_CHILD_ENV: Dict[str, str] = {}
if os.name == "nt":
    _CHILD_ENV = {
        "MSYS2_ARG_CONV_EXCL": "*",
        "MSYS_NO_PATHCONV": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
    }

_DECODE_ENCODINGS = ("utf-8", "gbk", "cp437", "cp1252")

# SDK 脚本输出的机器可读进度标记: [QPI-PROGRESS] total=.. done=.. pct=.. [label=..]
# label 可选: 当前阶段文案 (如下载/提取 sysroot), 由脚本自己决定并直接展示给用户;
# 插件不维护阶段名称, 各 SDK (M1/M2/H1/L1...) 可自定义自己的阶段。
_PROGRESS_RE = re.compile(
    r"\[QPI-PROGRESS\]\s+total=(\d+)\s+done=(\d+)\s+pct=(\d+)"
    r"(?:\s+label=(.*))?")


def _decode_out(raw) -> str:
    """把子进程输出解码为字符串 (UTF-8 优先, 回退 Windows OEM 代码页)。"""
    if isinstance(raw, str):
        return raw
    if not raw:
        return ""
    for enc in _DECODE_ENCODINGS:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def _pump_task_raw(proc: subprocess.Popen, task: Task) -> None:
    """原始字节透传: 不拆行、不去空白, 按块直接交给前端终端模拟器。

    bitbake (knotty) 交互式进度条依赖 `\\r` 原地重绘以及光标上移/擦除转义, 这些
    必须由完整终端模拟器处理。一旦按 \\r/\\n 拆行、再由前端用 "\\r + 行 + \\x1b[K"
    重拼, 光标列位置就错了 —— 紧接着的普通日志会被粘在进度条后面, 长进度条在窄
    终端还会折行使重绘失效, 表现出来就是"日志卡住"。

    用增量解码器跨块保留不完整的多字节字符, 避免把一个 UTF-8 字符劈成两半。
    """
    dec = codecs.getincrementaldecoder("utf-8")("replace")
    fd = proc.stdout.fileno()
    while not task._stop.is_set():
        try:
            chunk = os.read(fd, 8192)
        except OSError:
            break
        if not chunk:
            break
        text = dec.decode(chunk)
        if text:
            task.push_raw(text)
    tail = dec.decode(b"", final=True)
    if tail:
        task.push_raw(tail)


def _pump_task_output(proc: subprocess.Popen, task: Task) -> None:
    """把子进程输出逐行送入 task; 同时按 \n 与 \r 断行。

    必须按 \r 断行: curl/wget/aria2c 的下载进度条是 \r 原地刷新、不产生换行,
    若只按 \n 断行则整个下载期间收不到任何一行 —— 终端看起来像卡死, 前端 SSE
    也会因长时间无数据被 undici bodyTimeout 掐断 (ssh_client 早已按 \r 断行,
    本地/WSL 路径此前不一致)。
    \r 分隔出的空片段直接丢弃 (那是进度条重绘), \n 分隔的空行仍保留 (原文换行)。
    """
    if task.raw:
        _pump_task_raw(proc, task)
        return
    fd = proc.stdout.fileno()
    buf = b""
    while not task._stop.is_set():
        try:
            chunk = os.read(fd, 8192)
        except OSError:
            break
        if not chunk:
            break
        buf += chunk
        while True:
            nl = buf.find(b"\n")
            cr = buf.find(b"\r")
            if nl < 0 and cr < 0:
                break
            if cr >= 0 and (nl < 0 or cr < nl):
                idx, sep = cr, 1
                if cr + 1 < len(buf) and buf[cr + 1:cr + 2] == b"\n":
                    sep = 2
            else:
                idx, sep = nl, 1
            line = buf[:idx]
            sep_bytes = buf[idx:idx + sep]
            buf = buf[idx + sep:]
            text = _decode_out(line).strip()
            if text:
                task.push(text)
            elif b"\n" in sep_bytes:
                task.push("")
    if buf and not task._stop.is_set():
        text = _decode_out(buf).strip()
        if text:
            task.push(text)


def _find_bash() -> str:
    """解析 bash 的绝对路径 (带缓存)。

    Windows 上后端由 VS Code 进程拉起, PATH 里通常没有 git-bash;
    按裸名字 "bash" 启动 subprocess 会抛 WinError 2 (系统找不到指定的文件)。
    优先探测常见 Git for Windows 安装路径, 其次 PATH, 最后从 PATH 里的 git 反推。
    """
    global _BASH_CACHE
    if _BASH_CACHE:
        return _BASH_CACHE
    import shutil
    exe: Optional[str] = None
    if os.name == "nt":
        candidates = [
            r"C:\Program Files\Git\bin\bash.exe",
            r"C:\Program Files\Git\usr\bin\bash.exe",
            r"C:\Program Files (x86)\Git\bin\bash.exe",
            r"C:\Program Files (x86)\Git\usr\bin\bash.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Programs\Git\bin\bash.exe"),
            os.path.expandvars(r"%LOCALAPPDATA%\Programs\Git\usr\bin\bash.exe"),
            r"C:\msys64\usr\bin\bash.exe",
        ]
        for p in candidates:
            if os.path.isfile(p):
                exe = p
                break
        # Git 装在非标准位置 (如 D:/Git、E:/Git 便携版) 时固定候选都找不到:
        # 从 PATH 里的 git 可执行文件反推其自带的 bash (bin/bash.exe 或 usr/bin/bash.exe)
        if exe is None:
            git = shutil.which("git")
            if git:
                git_dir = os.path.dirname(os.path.abspath(git))
                for p in (
                    os.path.join(git_dir, "bash.exe"),
                    os.path.join(git_dir, "..", "bin", "bash.exe"),
                    os.path.join(git_dir, "..", "usr", "bin", "bash.exe"),
                ):
                    p = os.path.abspath(p)
                    if os.path.isfile(p):
                        exe = p
                        break
    if exe is None:
        exe = shutil.which("bash")
        # 排除 WSL 的 bash (System32/bash.exe): 它无法执行带 Windows 路径的
        # 本机命令 (烧录/拉取镜像/adb), 会让设备操作全部以 "command not found" 失败
        if exe and exe.lower().replace("/", "\\").endswith(r"\system32\bash.exe"):
            exe = None
    if not exe:
        raise FileNotFoundError(L(
            "找不到 bash (git-bash)。请安装 Git for Windows, "
            "或把 bash.exe 所在目录加入系统 PATH 后重启 VS Code"
        ))
    _BASH_CACHE = exe
    return exe


@dataclass
class Task:
    id: str
    cmd: str
    mode: str
    status: str = "running"
    exit_code: Optional[int] = None
    output: queue.Queue = field(default_factory=queue.Queue)
    created_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    _proc: Optional[subprocess.Popen] = None
    _ssh_stream: Optional[threading.Thread] = None
    _stop: threading.Event = field(default_factory=threading.Event)
    # 设备侧收尾命令 (可选): 任务被 kill 时在本机执行一次 (尽力而为)。
    # 用途: adb shell 里的远端进程不会随本机 adb 客户端退出而结束
    # (音频/视频播放会一直占着声卡与屏幕), 必须显式 pkill 收尾。
    kill_cmd: str = ""
    # 收尾回调 (可选): 任务被 kill 时异步执行 (尽力而为)。
    # 与 kill_cmd 的区别: kill_cmd 只在本机 git-bash 跑, 而这个回调由调用方提供,
    # 可经 executor 在 SDK 所在主机执行 —— 用于 SSH 远端也生效的 docker 容器收尾
    # (docker run 客户端被杀不会带走容器, 必须 docker rm -f)。
    kill_hook: Optional[Callable[[], None]] = None
    # raw=True: 输出按原始字节透传 (不按 \r/\n 拆行、不去空白), 交给前端终端模拟器
    # 自行处理转义。用于 pty 下的 bitbake 等交互式输出 —— 其进度条靠 \r 重绘 +
    # 光标上移/擦除实现, 人工拆行再由前端重拼无法等价于真实终端, 会把日志粘在
    # 进度条后面、或让进度条累积刷屏 (看起来像卡死)。
    raw: bool = False

    def push(self, line: str) -> None:
        # SDK 下载脚本输出的进度标记不入终端, 转为 progress 事件供前端渲染进度条
        m = _PROGRESS_RE.search(line)
        if m:
            ev = {"total": int(m.group(1)), "done": int(m.group(2)),
                  "pct": int(m.group(3))}
            label = (m.group(4) or "").strip()
            if label:
                ev["label"] = label
            try:
                self.output.put_nowait(("progress", ev))
            except (ValueError, queue.Full):
                pass
            return
        try:
            self.output.put_nowait(("out", line))
        except queue.Full:
            pass

    def push_err(self, line: str) -> None:
        try:
            self.output.put_nowait(("err", line))
        except queue.Full:
            pass

    def push_raw(self, text: str) -> None:
        """原样透传一段终端输出 (含 \\r 与 ANSI 转义), 前端直接写入终端。

        与 push 的区别: 不拆行、不裁空白、不解析进度标记 —— 完整保留 pty 的字节
        语义, 让终端模拟器 (xterm.js) 自己处理光标移动与原地刷新。
        """
        try:
            self.output.put_nowait(("raw", text))
        except queue.Full:
            pass

    def finish(self, code: int) -> None:
        self.exit_code = code
        # kill() 会先设置 _stop/_status；进程结束后这里不能把 killed 覆盖成 failed。
        # 竞态场景: kill() 杀进程 → 进程退出码非 0 → 若 finish() 先于 kill() 的
        # 状态标记执行, 会错误显示 "失败 (exit 1)"。
        if self.status != "killed":
            self.status = "killed" if self._stop.is_set() else ("done" if code == 0 else "failed")
        self.finished_at = time.time()
        self.push(f"[exit code: {code}]")

    def kill(self) -> None:
        self._stop.set()
        # 立即标记 killed: ① SSE 的 while task.status == "running" 循环立刻退出;
        # ② finish() 依据 status != "killed" 不会再把它覆盖成 failed。
        self.status = "killed"
        self.finished_at = time.time()
        # 清空已缓冲输出: cat 大文件时队列里可能积压几万行,
        # 不清空的话 SSE 收尾仍会把全部残留推给前端, 界面会持续滚动很久
        while True:
            try:
                self.output.get_nowait()
            except queue.Empty:
                break
        self.push("[killed]")
        if self._proc is not None:
            try:
                # 杀整个进程树 (编译/adb 会 fork 子进程)。Windows 没有 killpg，
                # 单杀 bash 会遗留 adb.exe，后续所有设备命令都会被占住。
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/PID", str(self._proc.pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        timeout=3,
                    )
                else:
                    os.killpg(os.getpgid(self._proc.pid), signal.SIGKILL)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
        if self._ssh_stream is not None:
            # SSH 任务: 流线程收到 _stop 后会主动关闭 channel, 远端进程随之中断
            self._ssh_stream.join(timeout=3)
        if self.kill_cmd:
            # 放最后且异步执行: 停止按钮要立即响应, 不能等收尾命令跑完
            threading.Thread(target=self._run_kill_cmd, daemon=True).start()
        if self.kill_hook is not None:
            # 同上: 异步执行; 由调用方决定在哪个主机执行 (本地/WSL/SSH)
            threading.Thread(target=self._run_kill_hook, daemon=True).start()

    def _run_kill_cmd(self) -> None:
        """执行设备侧收尾命令 (尽力而为: 失败只忽略, 不影响任务状态)。"""
        try:
            subprocess.run([_find_bash(), "-lc", self.kill_cmd],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=15)
        except Exception:
            pass

    def _run_kill_hook(self) -> None:
        """执行调用方提供的收尾回调 (尽力而为: 失败只忽略, 不影响任务状态)。"""
        try:
            if self.kill_hook is not None:
                self.kill_hook()
        except Exception:
            pass


class Executor:
    """统一命令执行入口。"""

    def __init__(self, conn: ConnectionConfig):
        self.conn = conn
        self._ssh: Optional[SSHClient] = None
        self._tasks: Dict[str, Task] = {}

    # ---------- 连接 ----------
    def ensure_ssh(self) -> Optional[SSHClient]:
        if self.conn.mode == "local":
            return None
        if self._ssh is None:
            self._ssh = SSHClient(
                host=self.conn.host, port=self.conn.port, user=self.conn.user,
                password=self.conn.password, key_path=self.conn.key_path,
            )
        self._ssh.connect()
        return self._ssh

    def test_connection(self) -> dict:
        """验证连接可用, 返回 SDK 目录信息。"""
        info: dict = {"mode": self.conn.mode, "ok": False}
        try:
            if self.conn.mode == "local":
                if self.conn.is_wsl:
                    # WSL 模式: 用 wsl.exe 验证发行版 + 发行版内 SDK 目录存在
                    code, out, err = self._exec_wsl_sync(
                        f"test -d {shlex.quote(self.conn.sdk_abs)} && echo EXISTS",
                        timeout=30)
                    if code != 0 or "EXISTS" not in out:
                        info["error"] = L(
                            "WSL 发行版 {distro} 中 SDK 目录不存在: {root}\n"
                            "请确认发行版名正确且 WSL 可启动 (wsl -d {distro} -- pwd)",
                            distro=self.conn.wsl_distro, root=self.conn.sdk_root)
                        return info
                    info["ok"] = True
                    info["sdk_root"] = self.conn.sdk_abs
                elif not self.conn.sdk_root or not os.path.isdir(self.conn.sdk_abs):
                    info["error"] = L("SDK 目录不存在: {root}", root=self.conn.sdk_root)
                    return info
                else:
                    info["ok"] = True
                    info["sdk_root"] = self.conn.sdk_abs
            else:
                ssh = self.ensure_ssh()
                # 握手 + 架构 + SDK 目录存在性合并为一次往返
                code, out, err = ssh.run(
                    f"echo ok && uname -m && test -d {shlex.quote(self.conn.sdk_root)} && echo EXISTS")
                if code != 0:
                    info["error"] = err or out
                    return info
                lines = [l for l in out.splitlines() if l.strip()]
                info["ok"] = True
                info["remote"] = f"{self.conn.user}@{self.conn.host}"
                info["arch"] = lines[0].strip() if lines else ""
                info["sdk_exists"] = "EXISTS" in out
                if not info["sdk_exists"]:
                    info["error"] = L("远端 SDK 目录不存在: {root}", root=self.conn.sdk_root)
                    return info
                info["sdk_root"] = self.conn.sdk_root
        except Exception as e:
            info["error"] = str(e)
        return info

    # ---------- 文件读取 ----------
    def read_file(self, path: str) -> str:
        if self.conn.mode == "local":
            # WSL 模式: path 是发行版内 POSIX 路径, 映射为 UNC 后本机读取。
            # 必须显式 utf-8: 中文 Windows 默认 locale 是 GBK, 不指定会把
            # UTF-8 的 template.json/main.c 等读成乱码。
            with open(self.conn.win_path(path), "r", encoding="utf-8",
                      errors="replace") as f:
                return f.read()
        ssh = self.ensure_ssh()
        return ssh.read_file(path)

    def read_sdk_file(self, rel_path: str) -> str:
        """读取 SDK 内文件 (自动拼 sdk_root)。"""
        return self.read_file(self.conn.join(self.conn.sdk_abs, rel_path))

    def write_file(self, path: str, content: str) -> None:
        """写入文件 (local/WSL 模式直接写; ssh 模式走 SFTP)。始终 UTF-8。"""
        if self.conn.mode == "local":
            with open(self.conn.win_path(path), "w", encoding="utf-8") as f:
                f.write(content)
            return
        ssh = self.ensure_ssh()
        sftp = ssh.ensure().open_sftp()
        try:
            with sftp.open(path, "w") as f:
                f.write(content)
        finally:
            sftp.close()

    def list_dir(self, path: str) -> List[dict]:
        if self.conn.mode == "local":
            path = self.conn.win_path(path)
            out = []
            for name in sorted(os.listdir(path)):
                full = os.path.join(path, name)
                st = os.stat(full)
                out.append({"name": name, "size": st.st_size,
                            "mtime": st.st_mtime,
                            "isdir": os.path.isdir(full)})
            return out
        ssh = self.ensure_ssh()
        return ssh.list_dir(path)

    def pull(self, remote_path: str, local_path: str) -> None:
        if self.conn.mode == "local":
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            import shutil
            shutil.copy2(self.conn.win_path(remote_path), local_path)
            return
        ssh = self.ensure_ssh()
        ssh.pull(remote_path, local_path)

    # ---------- 命令执行 ----------
    def exec(self, cmd: str, cwd: str = "", env: Optional[dict] = None,
             shell: bool = True, task_id: Optional[str] = None,
             input_data: Optional[str] = None, raw: bool = False) -> Task:
        """启动一个任务。cmd 为单条命令; cwd 相对 sdk_root 或绝对路径。

        input_data 非空时写入进程 stdin (用于 sudo -S 等从 stdin 读输入的场景)。
        raw=True 时输出原样透传 (见 Task.raw), 用于 pty 下的交互式输出。
        """
        if cwd and not self.conn.isabs(cwd):
            cwd = self.conn.join(self.conn.sdk_abs, cwd)
        elif not cwd:
            cwd = self.conn.sdk_abs  # 默认在 SDK 根目录执行
        tid = task_id or uuid.uuid4().hex[:12]
        task = Task(id=tid, cmd=cmd, mode=self.conn.mode, raw=raw)
        self._tasks[tid] = task

        if self.conn.mode == "local":
            if self.conn.is_wsl:
                self._exec_wsl(task, cmd, cwd, env, shell, input_data)
            else:
                self._exec_local(task, cmd, cwd, env, shell, input_data)
        else:
            self._exec_ssh(task, cmd, cwd, env, input_data)
        return task

    def exec_local(self, cmd: str, env: Optional[dict] = None,
                   shell: bool = True, task_id: Optional[str] = None,
                   input_data: Optional[str] = None) -> Task:
        """始终在本机执行 (SSH 模式下也用本地 subprocess)。

        用于设备操作: 本机烧录工具 / 本机 adb / 本机串口 AT。cwd 固定为本机后端工作目录。
        """
        tid = task_id or uuid.uuid4().hex[:12]
        task = Task(id=tid, cmd=cmd, mode=self.conn.mode + "/local")
        self._tasks[tid] = task
        self._exec_local(task, cmd, "", env, shell, input_data)
        return task

    def exec_sync_local(self, cmd: str, timeout: float = 60.0) -> tuple:
        """本机同步执行短命令 (SSH 模式也走本地), 返回 (exit_code, stdout, stderr)。"""
        try:
            proc = subprocess.run([_find_bash(), "-lc", cmd], capture_output=True,
                                  text=False,
                                  env={**os.environ, **_CHILD_ENV},
                                  timeout=timeout)
            return proc.returncode, _decode_out(proc.stdout), _decode_out(proc.stderr)
        except subprocess.TimeoutExpired as e:
            return -1, _decode_out(e.stdout), f"timeout after {timeout}s"

    # ---------- 设备操作执行 (按「设备连接位置」路由) ----------
    def exec_device(self, cmd: str, cwd: str = "", env: Optional[dict] = None,
                    shell: bool = True, input_data: Optional[str] = None,
                    task_id: Optional[str] = None) -> Task:
        """设备操作执行入口: 按 device_location 路由。

        - device_location=remote (ssh 模式): 走 exec (在远端编译机执行, 板子 USB 接远端)
        - 其余: 走 exec_local (本机执行, 板子 USB 接本机)
        """
        if self.conn.device_is_remote:
            return self.exec(cmd, cwd=cwd, env=env, shell=shell,
                             input_data=input_data, task_id=task_id)
        return self.exec_local(cmd, env=env, shell=shell, input_data=input_data,
                               task_id=task_id)

    def exec_sync_device(self, cmd: str, cwd: str = "", timeout: float = 60.0) -> tuple:
        """设备操作同步执行 (探测类短命令), 返回 (exit_code, stdout, stderr)。"""
        if self.conn.device_is_remote:
            return self.exec_sync(cmd, cwd=cwd, timeout=timeout)
        return self.exec_sync_local(cmd, timeout=timeout)


    def push(self, local_path: str, remote_path: str) -> None:
        """上传文件 (local 模式=复制; ssh 模式=SFTP)。"""
        if self.conn.mode == "local":
            os.makedirs(os.path.dirname(self.conn.win_path(remote_path)) or ".", exist_ok=True)
            import shutil
            shutil.copy2(local_path, self.conn.win_path(remote_path))
            return
        ssh = self.ensure_ssh()
        ssh.push(local_path, remote_path)

    # ---------- WSL 执行 (Windows 本机 + \\wsl.localhost\\ 路径) ----------
    def _wsl_argv(self, cmd: str, cwd: str, shell: bool) -> list:
        """构造 wsl.exe 启动参数: wsl.exe -d <发行版> --cd <cwd> -- bash -lc <cmd>。

        --cd 设置发行版内工作目录, 同时避免 wsl.exe 尝试翻译 Windows cwd 时
        输出 "Failed to translate" 噪声。SDK 命令 (编译/探测等) 都走这里。
        """
        argv = ["wsl.exe", "-d", self.conn.wsl_distro, "--cd", cwd or "/", "--"]
        if shell:
            argv += ["bash", "-lc", cmd]
        else:
            argv += shlex.split(cmd)
        return argv

    def _exec_wsl(self, task: Task, cmd: str, cwd: str,
                  env: Optional[dict], shell: bool,
                  input_data: Optional[str] = None) -> None:
        """WSL 模式的任务执行: 在发行版内以 bash 运行命令 (替代 git-bash)。"""

        def _run():
            try:
                proc = subprocess.Popen(
                    self._wsl_argv(cmd, cwd, shell),
                    env={**os.environ, **(env or {})},
                    stdin=subprocess.PIPE if input_data is not None else None,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=False,
                    start_new_session=True,  # 独立进程组, kill 时连子进程一起杀
                )
                task._proc = proc
                if input_data is not None and proc.stdin:
                    proc.stdin.write(input_data.encode())
                    proc.stdin.close()
                assert proc.stdout is not None
                _pump_task_output(proc, task)
                # 停止时兜底: kill() 可能早于 _proc 赋值或 taskkill 失败,
                # 这里确保进程树被强杀, 避免 adb/bash 残留占住后续命令
                if task._stop.is_set() and proc.poll() is None:
                    try:
                        if os.name == "nt":
                            subprocess.run(
                                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                timeout=5,
                            )
                        else:
                            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass
                proc.wait()
                task.finish(proc.returncode)
            except Exception as e:
                task.push_err(f"[executor error] {e}")
                task.finish(-1)
        threading.Thread(target=_run, daemon=True).start()

    def _exec_wsl_sync(self, cmd: str, cwd: str = "", env: Optional[dict] = None,
                       timeout: float = 60.0) -> tuple:
        """WSL 模式同步执行短命令, 返回 (exit_code, stdout, stderr)。"""
        try:
            proc = subprocess.run(
                self._wsl_argv(cmd, cwd, True),
                env={**os.environ, **(env or {})},
                capture_output=True, text=False,
                timeout=timeout)
            return proc.returncode, _decode_out(proc.stdout), _decode_out(proc.stderr)
        except subprocess.TimeoutExpired as e:
            return -1, _decode_out(e.stdout), f"timeout after {timeout}s"

    def _exec_local(self, task: Task, cmd: str, cwd: str,
                    env: Optional[dict], shell: bool,
                    input_data: Optional[str] = None) -> None:
        def _run():
            try:
                full_cmd = cmd if shell else shlex.split(cmd)
                proc = subprocess.Popen(
                    [_find_bash(), "-lc", full_cmd] if shell else full_cmd,
                    cwd=cwd or None,
                    env={**os.environ, **_CHILD_ENV, **(env or {})},
                    stdin=subprocess.PIPE if input_data is not None else None,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=False,
                    start_new_session=True,  # 独立进程组, kill 时连子进程一起杀
                )
                task._proc = proc
                if input_data is not None and proc.stdin:
                    proc.stdin.write(input_data.encode())
                    proc.stdin.close()
                assert proc.stdout is not None
                _pump_task_output(proc, task)
                # 停止时兜底: kill() 可能早于 _proc 赋值或 taskkill 失败,
                # 这里确保进程树被强杀, 避免 adb/bash 残留占住后续命令
                if task._stop.is_set() and proc.poll() is None:
                    try:
                        if os.name == "nt":
                            subprocess.run(
                                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                timeout=5,
                            )
                        else:
                            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass
                proc.wait()
                task.finish(proc.returncode)
            except Exception as e:
                task.push_err(f"[executor error] {e}")
                task.finish(-1)
        threading.Thread(target=_run, daemon=True).start()

    def _exec_ssh(self, task: Task, cmd: str, cwd: str,
                  env: Optional[dict], input_data: Optional[str] = None) -> None:
        def _run():
            try:
                ssh = self.ensure_ssh()
                push = task.push_raw if task.raw else task.push
                code, _ = ssh.stream(
                    cmd, cwd=cwd, env=env, stop_event=task._stop,
                    raw=task.raw,
                    line_cb=lambda line: push(line) if not task._stop.is_set() else None,
                    input_data=input_data,
                )
                if task._stop.is_set():
                    task.status = "killed"
                    task.finished_at = time.time()
                    task.push("[killed]")
                else:
                    task.finish(code)
            except Exception as e:
                task.push_err(f"[executor error] {e}")
                task.finish(-1)
        t = threading.Thread(target=_run, daemon=True)
        task._ssh_stream = t
        t.start()

    # ---------- 任务管理 ----------
    def exec_sync(self, cmd: str, cwd: str = "", env: Optional[dict] = None,
                  timeout: float = 60.0) -> tuple:
        """同步执行短命令, 返回 (exit_code, stdout, stderr)。用于探测类命令。"""
        if cwd and not self.conn.isabs(cwd):
            cwd = self.conn.join(self.conn.sdk_abs, cwd)
        elif not cwd:
            cwd = self.conn.sdk_abs
        if self.conn.mode == "local":
            if self.conn.is_wsl:
                return self._exec_wsl_sync(cmd, cwd, env, timeout)
            try:
                proc = subprocess.run(
                    [_find_bash(), "-lc", cmd], cwd=cwd or None,
                    env={**os.environ, **_CHILD_ENV, **(env or {})},
                    capture_output=True, text=False,
                    timeout=timeout)
                return proc.returncode, _decode_out(proc.stdout), _decode_out(proc.stderr)
            except subprocess.TimeoutExpired as e:
                return -1, _decode_out(e.stdout), f"timeout after {timeout}s"
        ssh = self.ensure_ssh()
        return ssh.run(cmd, cwd=cwd, env=env, timeout=timeout)

    def get_task(self, task_id: str) -> Optional[Task]:
        return self._tasks.get(task_id)

    def list_tasks(self) -> List[dict]:
        return [{
            "id": t.id, "cmd": t.cmd[:120], "mode": t.mode,
            "status": t.status, "exit_code": t.exit_code,
            "created_at": t.created_at, "finished_at": t.finished_at,
        } for t in self._tasks.values()]

    def kill_task(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        if task is None:
            return False
        task.kill()
        return True

    def drain(self, task: Task, timeout: float = 30.0) -> List[str]:
        """阻塞收集任务剩余输出 (用于非 SSE 的同步等待场景)。"""
        lines: List[str] = []
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                kind, line = task.output.get(timeout=0.5)
                lines.append(line)
            except queue.Empty:
                if task.status != "running":
                    break
        return lines
