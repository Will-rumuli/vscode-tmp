r"""连接配置数据模型。

支持两种连接模式:
- local: 本机 SDK (直接 subprocess)
- ssh:   远程编译机 SDK (paramiko)

local 模式下若 SDK 根目录是 WSL 共享路径 (如 \\wsl.localhost\Ubuntu\home\will\qpi\m2),
插件自动识别为 WSL 连接: 编译等 SDK 命令通过 wsl.exe 进入发行版执行,
设备操作 (adb/烧录) 仍在本机 Windows。连接模式仍是 local, 前端无需改动。
"""
from __future__ import annotations

import os
import posixpath
import re
from typing import Optional

from pydantic import BaseModel, Field, model_validator

from .i18n import L

# 匹配 Windows 上的 WSL 共享路径: \\wsl.localhost\<发行版>\<linux路径> 或 \\wsl$\<发行版>\<linux路径>
_WSL_UNC_RE = re.compile(
    r"^\\\\wsl(?:\.localhost|\$)\\([^\\]+)\\(.+)$", re.IGNORECASE)


class ConnectionConfig(BaseModel):
    mode: str = Field("local", description="local 或 ssh")
    # SSH 连接参数 (mode=ssh 时必填)
    host: str = Field("", description="SSH 主机")
    port: int = Field(22, description="SSH 端口")
    user: str = Field("", description="SSH 用户名")
    password: Optional[str] = Field(None, description="SSH 密码 (与 key 二选一)")
    key_path: Optional[str] = Field(None, description="SSH 私钥路径")
    # SDK 路径 (本地模式必填, ssh 模式为远端路径)
    sdk_root: str = Field("", description="SDK 根目录 (包含 build.sh 的目录)")
    # 工程代码创建目录 (相对 sdk_root)
    project_dir: str = Field("projects", description="工程创建目录, 相对 SDK 根目录")
    # WSL 发行版名 (检测到 \\wsl.localhost\ 路径时自动解析, 前端无需填写)
    wsl_distro: str = Field("", description="WSL 发行版名 (自动解析)")
    # 设备连接位置: local = 板子 USB 接本机, remote = 板子接远端编译机 (仅 ssh 模式有意义)。
    # 所有设备操作 (adb/AT/烧录/部署) 统一跟随该设置, 避免"烧录在远端、adb 在本机"的矛盾。
    device_location: str = Field("local", description="设备连接位置: local(本机) 或 remote(远端, 仅 ssh 模式)")

    @property
    def device_is_remote(self) -> bool:
        """设备操作是否在远端执行 (ssh 模式且设备连接位置选择远端)。"""
        return self.mode == "ssh" and self.device_location == "remote"

    @model_validator(mode="after")
    def _detect_wsl(self) -> "ConnectionConfig":
        """local 模式下 sdk_root 为 WSL 共享路径时, 自动解析出发行版名。"""
        if self.mode != "local" or not self.sdk_root:
            return self
        m = _WSL_UNC_RE.match(self.sdk_root.strip())
        if m:
            self.wsl_distro = m.group(1)
        return self

    # ---------- 路径辅助 (模式感知) ----------
    # 后端跑在 Windows 时, os.path 是 ntpath —— 对 ssh 模式的远端 POSIX 路径做
    # abspath/join 会把 /home/user/ai_sdk 变成 C:\home\user\ai_sdk, 或在路径中间
    # 插入反斜杠, 导致远端所有命令 (编译/探测/列目录) 失败。ssh 模式必须用 posixpath。
    # WSL 模式同理: 命令在发行版内执行, 路径必须保持 POSIX 形式。

    @property
    def is_wsl(self) -> bool:
        """是否通过 WSL 连接 (Windows 本机 + \\wsl.localhost\\... 路径)。"""
        return (self.mode == "local" and os.name == "nt" and bool(self.wsl_distro))

    @property
    def sdk_root_linux(self) -> str:
        """WSL 发行版内 SDK 根目录的 POSIX 路径 (如 /home/will/qpi/m2)。"""
        if not self.sdk_root:
            return ""
        m = _WSL_UNC_RE.match(self.sdk_root.strip())
        if m and m.group(2):
            return "/" + m.group(2).replace("\\", "/").rstrip("/")
        return ""

    @property
    def sdk_abs(self) -> str:
        """SDK 根目录绝对路径。

        - local 模式: Windows 路径
        - WSL 模式: 发行版内 POSIX 路径 (命令在 wsl.exe 里执行时必须用它)
        - ssh 模式: 远端 POSIX 路径, 保持原样
        """
        if not self.sdk_root:
            return ""
        if self.mode == "ssh":
            return self.sdk_root
        if self.is_wsl:
            return self.sdk_root_linux or ""
        return os.path.abspath(self.sdk_root)

    @property
    def sdk_abs_win(self) -> str:
        """SDK 根目录在 Windows 侧可访问的路径。

        - WSL 模式: UNC 路径 (本机 os 文件操作用它)
        - 其余模式: 与 sdk_abs 相同
        """
        if self.is_wsl:
            return os.path.abspath(self.sdk_root)
        return self.sdk_abs

    @property
    def projects_abs(self) -> str:
        return self.join(self.sdk_abs, self.project_dir)

    def join(self, *parts: str) -> str:
        """模式感知的路径拼接 (ssh/WSL 模式用 posixpath, 避免反斜杠)。"""
        return (posixpath if (self.mode == "ssh" or self.is_wsl) else os.path).join(*parts)

    def dirname(self, p: str) -> str:
        return (posixpath if (self.mode == "ssh" or self.is_wsl) else os.path).dirname(p)

    def basename(self, p: str) -> str:
        return (posixpath if (self.mode == "ssh" or self.is_wsl) else os.path).basename(p)

    def isabs(self, p: str) -> bool:
        """模式感知的绝对路径判断 (ssh/WSL 模式: 以 / 开头即绝对路径)。"""
        if self.mode == "ssh" or self.is_wsl:
            return p.startswith("/")
        return os.path.isabs(p)

    def win_path(self, linux_path: str) -> str:
        """把 WSL 发行版内的 POSIX 路径映射为 Windows 可访问的 UNC 路径。

        仅 WSL 模式生效 (本机 os 文件操作/Windows 侧工具传参用);
        其它模式原样返回。非 SDK 根目录下的路径也原样返回。
        """
        if not self.is_wsl:
            return linux_path
        root = self.sdk_root_linux
        if root and linux_path:
            if linux_path == root:
                return self.sdk_abs_win
            if linux_path.startswith(root + "/"):
                suffix = linux_path[len(root):].lstrip("/")
                return os.path.join(self.sdk_abs_win, *suffix.split("/")) \
                    if suffix else self.sdk_abs_win
        return linux_path

    def linux_path(self, win_path: str) -> str:
        """把 WSL 模式下的 Windows UNC 路径映射回发行版内 POSIX 路径 (win_path 的逆)。

        用于 WSL 内命令 (如 wsl.exe cp) 需要取 Linux 路径、而手里只有 UNC 路径时。
        """
        if not self.is_wsl:
            return win_path
        root_win = self.sdk_abs_win
        win_path = win_path.rstrip("\\")
        if root_win and win_path.lower().startswith(root_win.lower()):
            suffix = win_path[len(root_win):].strip("\\")
            if suffix:
                return posixpath.join(self.sdk_root_linux, *suffix.split("\\"))
            return self.sdk_root_linux
        return win_path

    def display(self) -> str:
        not_set = L("(未设置)")
        if self.mode == "local" and self.is_wsl:
            return f"WSL {self.wsl_distro} @ {self.sdk_abs or not_set}"
        if self.mode == "local":
            return f"local @ {self.sdk_root or not_set}"
        return f"ssh {self.user}@{self.host}:{self.port} @ {self.sdk_root or not_set}"
