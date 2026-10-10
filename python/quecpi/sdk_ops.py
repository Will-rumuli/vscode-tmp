"""M2 (RK3576) SDK 操作封装: 把 build.sh / tools 的能力包装成高层 API。

每个方法返回 Task (异步) 或直接返回结果 (同步探测类)。
所有命令都通过 Executor 执行, 自动适配本地/SSH。

覆盖能力:
- 编译: app / kernel / boot / overlays / rootfs / all (update.img) / check / clean
- 烧录: adb 部署运行 / rkdeveloptool / upgrade_tool / loader 探测
- 工程代码创建: 模板列表 / 创建(占位符替换) / 删除 / 列出
- 设备: adb devices / dmesg / journalctl / reboot
- AT 指令: 串口发送 (pyserial; WSL 宿主下自动转发到 Windows COM 口)
- 音频: tinymix / 日志抓取
- 工具: 截图 / 录屏 / 任意命令
- SKILL: skills/ 目录文档浏览
- 仓库引导: 组件缺失检测 / git clone
"""
from __future__ import annotations

import base64
import hashlib
import os
import posixpath
import re
import shlex
import sys
import time
from typing import Dict, List, Optional

from .executor import Executor, Task
from .i18n import L
from .models import ConnectionConfig, _WSL_UNC_RE

NAME_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")

# 烧录工具不随插件分发 (未签名 exe/adb 会触发商店安全扫描误报): Rockchip 平台的
# 宿主机烧录工具随 SDK 分发于 tools/flash/, 由 SDK 的 tools/VERSION.txt 声明,
# 插件按声明直接引用 (见 _flash_tools_from_sdk)。插件只内置 adb (见 _BUNDLED_ADB)。


def _bundled_tools_dir() -> str:
    """插件内置 tools/ 目录 (<ext>/tools)。可用环境变量 QPI_SDK_EXT_TOOLS 覆盖。"""
    env = os.environ.get("QPI_SDK_EXT_TOOLS")
    if env:
        return env
    # 本文件位于 <ext>/python/quecpi/sdk_ops.py -> 上溯两级得 <ext>, 再加 tools
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "tools"))


def _win_to_wsl_mnt(p: str) -> str:
    """Windows 路径 -> WSL 发行版内的 /mnt/<盘符小写>/... 路径 (供 wsl.exe 命令使用)。"""
    p = p.replace("\\", "/")
    m = re.match(r"^([A-Za-z]):/(.*)$", p)
    if m:
        drive, rest = m.group(1).lower(), m.group(2)
        return f"/mnt/{drive}/{rest}" if rest else f"/mnt/{drive}"
    return p


def _parse_wsl_unc(p: str) -> Optional[tuple]:
    r"""把 WSL 共享路径 (\\wsl.localhost\Ubuntu\home\... 或 \\wsl$\Ubuntu\...) 解析成 (发行版, linux路径)。

    非 WSL 路径返回 None。供 download_sdk 判断保存目录是否需要走 wsl.exe 执行。
    """
    m = _WSL_UNC_RE.match(p.strip())
    if not m:
        return None
    distro, linux = m.group(1), m.group(2).replace("\\", "/")
    return distro, "/" + linux.lstrip("/")


# ================= 平台识别 (SDK 类型) =================
# 各 SDK 在 tools/VERSION.txt 里声明平台标识 (M2: RK3576; H1: QCS6490 + Qualcomm EDL)。
# 插件据此选择烧录后端与进下载模式命令, 避免把 Rockchip 的流程套到 Qualcomm 上。
#
# 注意: 老 SDK (如 M2) 的 VERSION.txt 没有 FLASH_PROTOCOL/FLASH_ENTRY 字段,
# 此时一律回退到 Rockchip 默认行为, 保证既有功能不受影响。
#
# Rockchip 默认行为 (M2 及任何缺少平台字段的 SDK 都走这里)
_DEFAULT_PLATFORM: Dict[str, object] = {
    "board": "",
    "soc": "",
    "family": "rockchip",
    "kernel_dts": "",
    "vendor": "",
    "flash_protocol": "Rockchip rockusb",
    "flash_entry": "",           # 空 = 用插件内置 upgrade_tool/rkdeveloptool
    "enter_loader": "",          # 空 = adb reboot loader
    "update_img_supported": True,
    "source": "default",
}


def _to_mhz(raw: str) -> Optional[float]:
    """把 cpufreq 的 kHz 频率字符串转成 MHz (保留 1 位); 无效值返回 None。"""
    try:
        v = float(str(raw).split()[0])
    except (IndexError, TypeError, ValueError):
        return None
    return round(v / 1000.0, 1) if v > 0 else None


def _hz_to_mhz(raw: str) -> Optional[float]:
    """把 devfreq / RK gpu_freq 的 Hz 频率字符串转成 MHz (保留 1 位)。"""
    try:
        v = float(str(raw).split()[0])
    except (IndexError, TypeError, ValueError):
        return None
    return round(v / 1000000.0, 1) if v > 0 else None


def _parse_version_txt(text: str) -> Dict[str, str]:
    """解析 tools/VERSION.txt 的 key=value (忽略注释行与空行, 去引号)。"""
    kv: Dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if key:
            kv[key] = val.strip().strip('"').strip("'")
    return kv


def _classify_platform(kv: Dict[str, str]) -> Dict[str, object]:
    """由 VERSION.txt 字段归一化出平台信息。

    family:
      - "qualcomm": 声明了 Qualcomm/EDL 烧录协议, 或 SoC 为 QCS* 前缀 → 走 SDK 脚本
      - "rockchip": 其余 (含无任何平台字段的老 SDK) → 沿用插件内置 RK 工具
    """
    soc = kv.get("SOC", "").strip()
    proto = kv.get("FLASH_PROTOCOL", "").strip()
    upper = proto.upper()
    if "QUALCOMM" in upper or "EDL" in upper or soc.upper().startswith("QCS"):
        family = "qualcomm"
    else:
        family = "rockchip"
    # SDK_TYPE 区分构建体系: "yocto" (Yocto/BitBake, 如 Pi-H1-Linux-SDK) /
    # "simple" (原生 build.sh, 如 QPi-SDK H1-SDK 分支); 老 SDK 无声明按 simple
    sdk_type = kv.get("SDK_TYPE", "").strip().lower()
    if sdk_type != "yocto":
        sdk_type = "simple"
    return {
        "board": kv.get("BOARD", ""),
        "soc": soc,
        "family": family,
        # "yocto" = buildconfig/buildall/buildpackage 流程; "simple" = ./build.sh
        "sdk_type": sdk_type,
        "flash_protocol": proto or _DEFAULT_PLATFORM["flash_protocol"],
        # FLASH_ENTRY 形如 "scripts/flash.sh (自动判平台) / scripts/flash.bat (Windows)",
        # 只取第一个路径作为 Linux/WSL 入口
        "flash_entry": (kv.get("FLASH_ENTRY", "").split("(")[0].strip().split()[0]
                        if kv.get("FLASH_ENTRY", "").strip() else ""),
        # ENTER_EDL 形如 "adb shell reboot edl"; 存原样, 调用处按需去前导 adb
        "enter_loader": kv.get("ENTER_EDL", ""),
        "update_img_supported": kv.get("UPDATE_IMG_SUPPORTED", "").strip().lower() != "no",
        # KERNEL_DTS 声明板级主 .dts 文件名 (两套 SDK 都有, 如 H1: qcs6490-idp-pi)
        "kernel_dts": kv.get("KERNEL_DTS", ""),
        "vendor": kv.get("VENDOR", ""),
        # 构建体系入口: yocto = source 入口脚本 (如 quectel_build/compile/build.sh)
        "build_entry": kv.get("BUILD_ENTRY", ""),
        # 烧录固件目录 (相对 SDK 根, 可选; 未声明时按 flash 脚本结构探测)。
        # 供 ssh 模式 + 本机设备时把固件拉取到本机烧录。
        "flash_fw_dir": kv.get("FLASH_FW_DIR", "").strip(),
        # 宿主机烧录工具 (Rockchip 平台): 随 SDK 分发于 tools/flash/, 插件直接引用,
        # 不再内置也不再联网下载。相对 SDK 根; 未声明时回退 PATH 探测 (兼容旧 SDK)。
        "flash_tools_linux": kv.get("FLASH_TOOLS_LINUX", "").strip(),
        "flash_tools_mac": kv.get("FLASH_TOOLS_MAC", "").strip(),
        "flash_tools_windows": kv.get("FLASH_TOOLS_WINDOWS", "").strip(),
        "flash_tools_windows_gui": kv.get("FLASH_TOOLS_WINDOWS_GUI", "").strip(),
        "source": "VERSION.txt",
    }


# 设备侧平台识别: 由设备自身 /proc/device-tree/compatible 判定**真实**板卡/SoC。
# 与 _classify_platform (读 SDK 的 tools/VERSION.txt) 不同 —— 后者描述的是所连
# SDK 声明的平台。当"连的是 H1 SDK、实际插的是 M2 板"时, 设备操作场景 (性能监控)
# 应显示真实插入的板子, 故此处以设备端信息为准。
_DEVICE_SOC_MAP = (
    # (compatible 关键字, board, soc, family)
    (("rk3576",), "M2", "RK3576", "rockchip"),
    (("qcs6490", "qcm6490"), "H1", "QCS6490", "qualcomm"),
)


def _platform_from_device(model: str, compat: List[str]) -> Optional[Dict[str, object]]:
    """由设备端 model / compatible 推断真实板卡与 SoC; 无法识别时返回 None。

    compatible 形如 ["quectel,pi-m2-rk3576", "rockchip,rk3576"] 或
    ["qcom,qcs6490-idp", "qcom,qcs6490"]; 命中已知板卡表时给出 board/soc,
    否则退化为按 SoC 前缀 (rk/qcs/qcm/sm/sc) 提取 soc, board 用 model 兜底。
    """
    blob = " ".join(compat).lower() or model.lower()
    if not blob:
        return None
    board = soc = family = ""
    for keys, b, s, fam in _DEVICE_SOC_MAP:
        if any(k in blob for k in keys):
            board, soc, family = b, s, fam
            break
    if not soc:
        m = re.search(r"\b(rk\d{3,4}[a-z]?)\b", blob)
        if m:
            soc, family = m.group(1).upper(), "rockchip"
        else:
            m = re.search(r"\b((?:qcs|qcm|sm|sc)\d{3,4})\b", blob)
            if m:
                soc, family = m.group(1).upper(), "qualcomm"
    if not soc:
        return None
    return {"board": board or model.strip(), "soc": soc,
            "family": family, "source": "device"}


# ================= 设备树 (DTS/DTSI) 解析 =================

def _mask_dts_comments(text: str) -> str:
    """把 DTS 中的 //、/* */ 注释和 "..." 字符串替换为等长空格。

    保留换行与花括号结构, 供结构扫描 (块匹配/属性行判定) 使用,
    避免注释或字符串里的 { } ; 干扰解析。
    """
    out = list(text)
    n = len(text)
    i = 0
    while i < n:
        c = text[i]
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            j = text.find("\n", i)
            if j < 0:
                j = n
            for k in range(i, j):
                out[k] = " "
            i = j
        elif c == "/" and i + 1 < n and text[i + 1] == "*":
            j = text.find("*/", i + 2)
            if j < 0:
                j = n
            else:
                j += 2
            for k in range(i, j):
                out[k] = " "
            i = j
        elif c == '"':
            j = i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == '"':
                    break
                j += 1
            for k in range(i, min(j + 1, n)):
                out[k] = " "
            i = j + 1
        else:
            i += 1
    return "".join(out)


_DTS_BLOCK_RE = re.compile(r"(?m)^\s*&([A-Za-z0-9_\-]+)\s*\{")


def _find_dts_blocks(content: str) -> List[dict]:
    """扫描 DTS 内容中的 &label { ... }; 覆盖块 (跳过 / { } 根块与 &{/} 根引用)。

    返回 [{start, body_start, end, label}]:
    - start: 块起始 (含 &label)
    - body_start: 打开花括号之后的位置
    - end: 闭合花括号 '}' 之后的位置 (含 '};')
    """
    masked = _mask_dts_comments(content)
    blocks: List[dict] = []
    i = 0
    n = len(masked)
    while i < n:
        m = _DTS_BLOCK_RE.search(masked, i)
        if not m:
            break
        start = m.start()
        brace = m.end() - 1
        if brace < 0:
            break
        depth = 1
        j = brace + 1
        while j < n and depth > 0:
            c = masked[j]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
            j += 1
        blocks.append({
            "start": start,
            "body_start": brace + 1,
            "end": j,
            "label": m.group(1),
        })
        i = j
    return blocks


def _dts_block_props(body: str) -> Dict[str, str]:
    """从块体文本 (不含开头的 &label { 与结尾的 }; ) 提取顶层属性。

    返回 {key: 值 或 ''(布尔属性)}。只取直接位于块内的属性,
    子节点内部 (嵌套花括号) 的属性忽略。支持跨行属性值 (以 ';' 收尾)。
    """
    masked = _mask_dts_comments(body)
    props: Dict[str, str] = {}
    depth = 1
    pending_key: Optional[str] = None
    pending_val: List[str] = []
    for ml, ol in zip(masked.splitlines(), body.splitlines()):
        ms = ml.strip()
        os_ = ol.strip()
        if not ms:
            continue
        opens = ms.count("{")
        closes = ms.count("}")
        if depth == 1 and opens == 0:
            if pending_key is None:
                # 单行完整属性: key = value;
                m = re.match(r"^([^\s=;{}/]+)\s*=\s*(.*?)\s*;\s*$", os_)
                if m:
                    props[m.group(1).strip()] = m.group(2).strip()
                    depth += opens - closes
                    if depth <= 0:
                        break
                    continue
                # 布尔属性: key;
                m = re.match(r"^([^\s=;{}/]+);\s*$", os_)
                if m:
                    props[m.group(1).strip()] = ""
                    depth += opens - closes
                    if depth <= 0:
                        break
                    continue
                # 跨行属性起始: key = value (未以 ';' 收尾)
                m = re.match(r"^([^\s=;{}/]+)\s*=\s*(.*)$", os_)
                if m:
                    pending_key = m.group(1).strip()
                    pending_val = [m.group(2).strip()]
                    depth += opens - closes
                    if depth <= 0:
                        break
                    continue
            else:
                # 收集跨行属性值, 直到遇到 ';'
                pending_val.append(os_)
                if ms.endswith(";"):
                    props[pending_key] = \
                        " ".join(v for v in pending_val if v).rstrip(";")
                    pending_key = None
                depth += opens - closes
                if depth <= 0:
                    break
                continue
        depth += opens - closes
        if depth <= 0:
            break
    if pending_key is not None:
        props[pending_key] = \
            " ".join(v for v in pending_val if v).rstrip(";")
    return props


def _dts_block_base_indent(body: str) -> str:
    """取块内首个顶层属性行的缩进 (用于新增属性行对齐), 默认制表符。"""
    masked = _mask_dts_comments(body)
    depth = 1
    for ml, ol in zip(masked.splitlines(), body.splitlines()):
        ms = ml.strip()
        if not ms:
            continue
        opens = ms.count("{")
        closes = ms.count("}")
        if depth == 1 and opens == 0 and ms.endswith(";"):
            indent = ol[:len(ol) - len(ol.lstrip())]
            return indent or "\t"
        depth += opens - closes
        if depth <= 0:
            break
    return "\t"


def _parse_props_text(text: str) -> Dict[str, str]:
    """把前端传来的属性文本解析成 {key: 值 或 ''}。

    每行一个属性: `key = value;` 或布尔属性 `key;`。跳过注释与删除指令。
    """
    props: Dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("//") or line.startswith("/*") \
                or line.startswith("/delete-property/"):
            continue
        m = re.match(r"^([^\s=;{}/]+)\s*=\s*(.*?)\s*;\s*$", line)
        if m:
            props[m.group(1).strip()] = m.group(2).strip()
            continue
        m = re.match(r"^([^\s=;{}/]+);\s*$", line)
        if m:
            props[m.group(1).strip()] = ""
    return props


def _rewrite_block_props(content: str, label: str, props: Dict[str, str]) -> str:
    """替换 &label 块的顶层属性为 props (保留子节点与注释), 返回新内容。"""
    blocks = _find_dts_blocks(content)
    target = next((b for b in blocks if b["label"] == label), None)
    if target is None:
        raise ValueError(L("未在设备树源文件中找到节点 &{label}", label=label))
    body = content[target["body_start"]:target["end"]]
    inner = body.lstrip("\r\n")
    inner_lines = inner.splitlines()
    masked = _mask_dts_comments(inner)
    base_indent = "\t"
    prop_idx: List[int] = []
    depth = 1
    for idx, (ml, ol) in enumerate(zip(masked.splitlines(), inner_lines)):
        ms = ml.strip()
        if not ms:
            continue
        opens = ms.count("{")
        closes = ms.count("}")
        if depth == 1 and opens == 0 and ms.endswith(";"):
            if not prop_idx:
                indent = ol[:len(ol) - len(ol.lstrip())]
                base_indent = indent or "\t"
            prop_idx.append(idx)
        depth += opens - closes
        if depth <= 0:
            break
    keep = [ol for idx, ol in enumerate(inner_lines) if idx not in set(prop_idx)]
    new_props = [
        f"{base_indent}{k} = {v};" if v else f"{base_indent}{k};"
        for k, v in props.items()
    ]
    new_inner = "\n".join(new_props + keep)
    return content[:target["body_start"]] + "\n" + new_inner + "\n" + content[target["end"]:]


class SDK:
    def __init__(self, conn: ConnectionConfig):
        self.conn = conn
        self.executor = Executor(conn)
        # 烧录工具注册表: 工具名 -> {name, kind, found, path, ...}
        self._tool_registry: Dict[str, dict] = {}
        # 本机 adb 可执行文件缓存 (PATH 或插件内置)
        self._adb_exe_cached: Optional[str] = None
        # 解析到的 adb 是否为 Windows 可执行文件 (WSL 经 interop 调用): 推送源路径需转 UNC
        self._adb_is_windows: bool = False
        # 板级 DTS/DTSI 内容缓存: walk 与 parse 都读同一批文件, 避免重复读取
        self._dts_cache: Dict[str, str] = {}
        # 平台识别结果缓存 (读取一次 tools/VERSION.txt 后固定)
        self._platform_cached: Optional[Dict[str, object]] = None
        # 性能监控: 上一次 /proc/stat 采样 (算 CPU 占用率需要相邻两次的差值)
        self._perf_prev: Optional[Dict[str, float]] = None
        # Docker 调用方式探测缓存: (prefix, need_password); prefix ∈ {"", "sudo"}
        self._docker_entry_cached: Optional[tuple] = None
        # SDK 所在主机 OS 家族探测缓存 (linux / darwin / ...), 决定 docker run 参数
        self._host_os_cached: Optional[str] = None

    # ================= 路径辅助 =================
    def _abs(self, rel: str) -> str:
        return self.conn.join(self.conn.sdk_abs, rel) if rel else self.conn.sdk_abs

    def _read_dts(self, path: str) -> str:
        """读板级 DTS 文件 (带缓存); 写操作后需 _invalidate_dts 失效。"""
        text = self._dts_cache.get(path)
        if text is None:
            text = self.executor.read_file(path)
            self._dts_cache[path] = text
        return text

    def _invalidate_dts(self, path: str) -> None:
        self._dts_cache.pop(path, None)

    # ================= 平台识别 =================
    def platform(self) -> Dict[str, object]:
        """识别当前连接的 SDK 属于哪个平台 (H1/Qualcomm 还是 M2/Rockchip)。

        读取 <sdk_root>/tools/VERSION.txt 并归一化; 文件缺失或无平台字段时
        返回 Rockchip 默认值, 保证既有 SDK 行为不变。结果按连接缓存。
        """
        if self._platform_cached is not None:
            return self._platform_cached
        plat: Dict[str, object] = dict(_DEFAULT_PLATFORM)
        try:
            content = self.executor.read_file(self._abs("tools/VERSION.txt"))
            kv = _parse_version_txt(content)
            if kv:
                plat = _classify_platform(kv)
        except Exception:
            # 文件不存在/不可读: 保持默认 (Rockchip), 不因探测失败影响主流程
            pass
        self._platform_cached = plat
        return plat

    # ---- Qualcomm (H1): 烧录委托给 SDK 自带脚本 ----
    def _flash_tools_sdk_script(self, plat: Dict[str, object]) -> List[dict]:
        """Qualcomm 平台的烧录工具: SDK 自带的全量烧录脚本 (如 scripts/flash.sh)。

        脚本内部已自行判定平台 (Linux/WSL 走 qdl; WSL 未直通 USB 时自动转发到
        Windows 的 scripts/flash.bat / QFIL 后端), 插件不重复实现 qdl/QFIL。
        """
        rel = str(plat.get("flash_entry") or "scripts/flash.sh")
        entry: dict = {
            "name": rel,
            "kind": "sdk-script",
            "hint_key": "flash.hint.sdkScript",
            "hint_vars": {
                "board": str(plat.get("board") or ""),
                "proto": str(plat.get("flash_protocol") or ""),
            },
            "found": False,
            "path": "",
        }
        script = self._abs(rel)
        if self._exists(script):
            entry["found"] = True
            entry["path"] = script
        self._tool_registry[entry["name"]] = entry
        return [entry]

    def _flash_via_sdk_script(self, plat: Dict[str, object],
                              password: str = "") -> Task:
        """Qualcomm 全量烧录: 调用 SDK 自带脚本 (如 scripts/flash.sh)。

        为什么委托 SDK 脚本: flash.sh 已完整处理 EDL(9008) 等待、qdl/QFIL 后端选择、
        WSL 到 Windows 的自动转发与固件完整性校验; 插件重复实现既易错也难与 SDK 同步。

        执行位置按「设备连接位置」:
        - ssh 模式 + 本机设备: 把烧录脚本/工具/固件拉取到本机临时目录后本地执行
          (板子 USB 接本机; Windows 下 flash.sh 会自动转发到 flash.bat QFIL 后端)
        - 其余: 直接在 SDK 所在机器执行 (本机 SDK 或远端编译机)

        password: sudo 密码 (仅远端设备模式)。flash.sh 内部裸 sudo (qdl 需 root),
        SSH 无 tty 时读不到密码, 故注入 SUDO_ASKPASS 助手 + stdin read 注入
        (密码不落远端 argv); 免密 sudo 时传空串即可。
        """
        rel = str(plat.get("flash_entry") or "scripts/flash.sh")
        script = self._abs(rel)
        if not self._exists(script):
            raise ValueError(
                L("未找到 SDK 烧录脚本: {script}\n请确认已连接 {board} SDK (含 {rel})", script=script, board=plat.get('board') or 'Qualcomm', rel=rel))
        if self.conn.mode == "ssh" and not self.conn.device_is_remote:
            return self._flash_sdk_script_local(plat, rel)
        cmd = (f"cd {shlex.quote(self.conn.sdk_abs)} && "
               f"bash {shlex.quote(rel)}")
        if password and self.conn.mode == "ssh":
            # 远端设备模式: 注入 askpass, 让脚本内裸 sudo 也能读到密码。
            # 注意: 仅设 SUDO_ASKPASS 环境变量不会生效 —— sudo 必须显式 -A
            # (或 sudoers 配 askpass) 才会调用 askpass 助手, 裸 sudo 在无 tty 下
            # 仍会报 "a terminal is required to read the password"。
            # 因此用 bash 函数覆盖 sudo, 自动追加 -A, 并经 export -f 传给 flash.sh。
            # 密码经 stdin read 注入 (不落远端 argv); askpass 助手由 sudo 以
            # 当前用户进程环境执行, 能读到 QPI_SUDO_PASS。
            askpass = "$HOME/.qpi_tools/askpass.sh"
            prefix = (
                f"mkdir -p $HOME/.qpi_tools && "
                f"printf '%s\\n' '#!/bin/sh' 'echo \"$QPI_SUDO_PASS\"' > {askpass} && "
                f"chmod +x {askpass} && "
                f"read -r QPI_SUDO_PASS && export QPI_SUDO_PASS && "
                f"export SUDO_ASKPASS={askpass} && "
                f"export _QPI_SUDO_BIN=$(command -v sudo) && "
                f"sudo() {{ \"$_QPI_SUDO_BIN\" -A \"$@\"; }} && "
                f"export -f sudo && "
            )
            sdk = shlex.quote(self.conn.sdk_abs)
            cmd = f"cd {sdk} && bash {shlex.quote(rel)}"
            return self.executor.exec(prefix + cmd, input_data=password + "\n")
        return self.executor.exec(cmd)

    def _h1_fw_dir_remote(self, plat: Dict[str, object],
                          entry_rel: str) -> str:
        """在远端解析 H1 烧录固件目录 (含 prog_firehose_*.elf 与 partition_ufs/ 的目录)。

        候选 (按声明/结构依次探测, 命中第一个同时含 firehose 与 partition 的):
        1) VERSION.txt 的 FLASH_FW_DIR (显式声明, 相对 SDK 根)
        2) <sdk>/<QUECTEL_PROJECT_REV> (yocto: tools/flash.sh 从 buildconfig 读)
        3) <sdk>/build/result (simple: scripts/env.sh 的 OUT_DIR 默认值)
        4) <sdk>/quectel_build/output (Yocto 输出兜底)
        """
        sdk = self.conn.sdk_abs
        cands: List[str] = []
        fw_rel = str(plat.get("flash_fw_dir") or "").strip()
        if fw_rel:
            cands.append(self.conn.join(sdk, fw_rel))
        if entry_rel.startswith("tools/"):
            cfg = self.conn.join(sdk, "compile/quectel-features-config/quectel-buildconfig-gen.h")
            code, out, err = self.executor.exec_sync(
                f"grep -m1 'QUECTEL_PROJECT_REV' {shlex.quote(cfg)} 2>/dev/null "
                f"| sed 's/.*\"\\(.*\\)\".*/\\1/'", timeout=15)
            rev = out.strip()
            if rev:
                cands.append(self.conn.join(sdk, rev))
        cands += [self.conn.join(sdk, "build/result"),
                  self.conn.join(sdk, "quectel_build/output")]
        probe = ("for d in " + " ".join(shlex.quote(c) for c in cands) + "; do "
                 "[ -d \"$d\" ] || continue; "
                 "ls \"$d\"/prog_firehose_*.elf >/dev/null 2>&1 || continue; "
                 "ls -d \"$d\"/partition_ufs \"$d\"/partition_emmc >/dev/null 2>&1 "
                 "&& { echo \"FW:$d\"; break; }; "
                 "done")
        code, out, err = self.executor.exec_sync(probe, timeout=30)
        for line in out.splitlines():
            if line.startswith("FW:"):
                return line[3:].strip()
        raise ValueError(
            L("无法在编译机定位 H1 烧录固件目录 (需同时含 prog_firehose_*.elf 与 "
            "partition_ufs/ 或 partition_emmc/)。\n"
            "请先在编译机完成「全量编译」生成固件; 若固件目录非默认位置, 可在 "
            "tools/VERSION.txt 增加声明: FLASH_FW_DIR=<相对 SDK 的固件目录>"))

    def _pull_tree_cmd(self, pairs: List[tuple]) -> str:
        """生成「递归拉取多个远端目录/文件到本机」的命令 (带实时进度)。

        pairs: [(远端路径, 本机暂存路径), ...]。远端不存在的条目跳过并输出
        PULL_SKIP 提示; 整体按已传字节输出 PULL_PROGRESS:n (每 5%), 完成后
        输出 PULL_DONE。用后端 venv python 内联 paramiko, 与 _pull_cmd 同一机制。
        """
        import tempfile
        venv_py = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            ".venv",
            "Scripts" if os.name == "nt" else "bin",
            "python.exe" if os.name == "nt" else "python",
        )
        if not os.path.isfile(venv_py):
            venv_py = sys.executable
        NL = chr(10)
        py = (
            "import paramiko,sys,os" + NL +
            "host,port,user=sys.argv[1],int(sys.argv[2]),sys.argv[3]" + NL +
            "pw=sys.argv[4] or None" + NL +
            "key=os.path.expanduser(sys.argv[5]) if sys.argv[5] else None" + NL +
            "pairs=[a.split('|',1) for a in sys.argv[6:]]" + NL +
            "def walk(s,r,l,out):" + NL +
            "  try:" + NL +
            "    for a in s.listdir_attr(r):" + NL +
            "      rp=r+'/'+a.filename; lp=os.path.join(l,a.filename)" + NL +
            "      if a.st_mode & 0o040000:" + NL +
            "        os.makedirs(lp,exist_ok=True); walk(s,rp,lp,out)" + NL +
            "      else: out.append((rp,lp,a.st_size))" + NL +
            "  except IOError: print('PULL_SKIP: 远端目录不可读: '+r,flush=True)" + NL +
            "c=paramiko.SSHClient();c.set_missing_host_key_policy(paramiko.AutoAddPolicy())" + NL +
            "try:" + NL +
            "  c.connect(host,port,user,password=pw,key_filename=key,timeout=15)" + NL +
            "  s=c.open_sftp(); files=[]" + NL +
            "  for r,l in pairs:" + NL +
            "    try: st=s.stat(r)" + NL +
            "    except IOError: print('PULL_SKIP: 远端不存在: '+r,flush=True); continue" + NL +
            "    if st.st_mode & 0o040000:" + NL +
            "      os.makedirs(l,exist_ok=True); walk(s,r,l,files)" + NL +
            "    else:" + NL +
            "      os.makedirs(os.path.dirname(l) or '.',exist_ok=True); files.append((r,l,st.st_size))" + NL +
            "  nfiles=len(files)" + NL +
            "  for i,(r,l,sz) in enumerate(files,1):" + NL +
            "    print('PULL_PROGRESS:'+str(int(i*100.0/nfiles))+' '+os.path.basename(r),flush=True)" + NL +
            "    os.makedirs(os.path.dirname(l) or '.',exist_ok=True)" + NL +
            "    try:" + NL +
            "      s.get(r,l)" + NL +
            "    except IOError as e:" + NL +
            "      print('PULL_ERROR: 拉取失败 '+r+': '+repr(e),flush=True); sys.exit(1)" + NL +
            "  print('PULL_DONE',flush=True)" + NL +
            "except Exception as e:" + NL +
            "  print('PULL_ERROR: 连接或拉取失败: '+repr(e),flush=True); sys.exit(1)" + NL +
            "finally:" + NL +
            "  try: s.close()" + NL +
            "  except Exception: pass" + NL +
            "  try: c.close()" + NL +
            "  except Exception: pass" + NL
        )
        args = (self.conn.host, str(self.conn.port), self.conn.user,
                self.conn.password or "", self.conn.key_path or "")
        pair_args = [f"{r}|{l}" for r, l in pairs]
        return (f"{shlex.quote(venv_py)} -c {shlex.quote(py)} "
                + " ".join(shlex.quote(a) for a in args)
                + (" " + " ".join(shlex.quote(a) for a in pair_args) if pair_args else ""))

    def _flash_sdk_script_local(self, plat: Dict[str, object],
                                rel: str) -> Task:
        """ssh 模式 + 本机设备 (H1): 把烧录脚本/工具/固件拉取到本机后本地执行。

        拉取清单 (按 SDK 相对路径镜像到本机临时目录, 保持 flash 脚本对路径的
        原有解析不变):
        - flash 入口所在目录 (scripts/ 或 tools/)
        - tools/ (含 qdl / qfil / adb)
        - compile/ (yocto 版 flash.sh 读 buildconfig, 存在才拉)
        - 固件目录 (如 build/result 或 <rev>)
        执行: 在本机 bash 运行 flash 入口脚本 (Windows 下 git-bash 自动判定并
        转发到 flash.bat QFIL 后端), 板子 USB 接本机。
        """
        import tempfile
        import posixpath
        sdk = self.conn.sdk_abs
        fw = self._h1_fw_dir_remote(plat, rel)
        base = re.sub(r"[^A-Za-z0-9_\-]", "_",
                      (sdk.rstrip("/").split("/")[-1] or "sdk"))
        stage = os.path.join(tempfile.gettempdir(), "qpi_h1_flash", base)
        os.makedirs(stage, exist_ok=True)
        entry_dir = posixpath.dirname(rel) or "."
        fw_rel = posixpath.relpath(fw, sdk)
        if fw_rel in (".", "..") or fw_rel.startswith("../"):
            raise ValueError(L("固件目录不在 SDK 内, 无法拉取: {fw}", fw=fw))
        pairs = [
            (self.conn.join(sdk, entry_dir), os.path.join(stage, entry_dir)),
            (self.conn.join(sdk, "tools"), os.path.join(stage, "tools")),
            (fw, os.path.join(stage, fw_rel)),
        ]
        # compile/ 仅 yocto 版 flash.sh 需要 (读 buildconfig), 远端存在才拉取
        if self._exists(self.conn.join(sdk, "compile")):
            pairs.insert(2, (self.conn.join(sdk, "compile"),
                             os.path.join(stage, "compile")))
        pull = self._pull_tree_cmd(pairs)
        cmd = (L("echo '[QPi] 正在从编译机拉取 H1 烧录脚本与固件到本机 (固件较大, 请稍候)...'; {pull} && echo PULL_OK && cd {dir} && bash {script} ufs", pull=pull, dir=shlex.quote(os.path.join(stage, entry_dir)), script=shlex.quote(os.path.basename(rel))))
        return self.executor.exec_local(cmd)

    # ================= 信息探测 =================
    def probe(self) -> dict:
        """探测 SDK 基本信息: 目录结构 / 组件完整性 / 工具链 / adb。"""
        info: dict = {"sdk_root": self.conn.sdk_abs, "ok": False, "components": {}}
        # 平台标识 (H1/Qualcomm 或 M2/Rockchip): 供前端决定烧录入口与进下载模式文案
        info["platform"] = self.platform()
        try:
            # 组件完整性 (用于仓库引导页): 合并为一次命令,
            # 避免 WSL/ssh 下逐项串行往返 (WSL 每次 wsl.exe 启动 ~0.5-1s)
            names = ("build.sh", "kernel", "tools", "prebuilds", "toolchains",
                     "docs/templates", "docs/examples", "overlay", "skills")
            checks = "; ".join(
                f'[ -e {shlex.quote(self._abs(n))} ] && echo "{n}=1" || echo "{n}=0"'
                for n in names)
            code, out, err = self.executor.exec_sync(checks, timeout=30)
            for line in out.splitlines():
                line = line.strip()
                if not line or "=" not in line:
                    continue
                name, _, mark = line.partition("=")
                info["components"][name] = mark == "1"
            # 模板数量
            tpl = self.list_templates()
            info["template_count"] = len(tpl)
            info["has_build_sh"] = info["components"].get("build.sh", False)
            info["ok"] = info["has_build_sh"]
            # adb (本机执行; 未安装时给出提示而非报错)
            try:
                info["adb_devices"] = self.adb_devices()
            except Exception as e:
                info["adb_error"] = str(e)
        except Exception as e:
            info["error"] = str(e)
        return info

    def _exists(self, path: str) -> bool:
        """检查远端/本地路径是否存在。

        ssh 模式: 用 SFTP stat 检查 (与镜像拉取/文件读写同一机制, 避免 shell
        test -e 与 SFTP 路径解析不一致导致的误判)。
        """
        if self.conn.mode == "ssh":
            try:
                ssh = self.executor.ensure_ssh()
                sftp = ssh.ensure().open_sftp()
                try:
                    sftp.stat(path)
                    return True
                except IOError:
                    return False
                finally:
                    sftp.close()
            except Exception:
                return False
        try:
            code, out, err = self.executor.exec_sync(
                f"test -e {shlex.quote(path)} && echo 1", timeout=15)
            return out.strip() == "1"
        except Exception:
            return False

    # ================= 模板 & 工程代码创建 =================
    def list_templates(self) -> List[dict]:
        """列出 docs/templates 下的模板 (读取 template.json)。"""
        tpl_dir = self._abs("docs/templates")
        code, out, err = self.executor.exec_sync(
            f"ls -d {shlex.quote(tpl_dir)}/*/ 2>/dev/null", timeout=15)
        dirs = [d.strip() for d in out.splitlines() if d.strip()]
        templates: List[dict] = []
        for d in dirs:
            meta = self.conn.join(d, "template.json")
            try:
                content = self.executor.read_file(meta)
                import json
                t = json.loads(content)
                t["id"] = t.get("id") or self.conn.basename(d.rstrip("/"))
                t["path"] = d
                templates.append(t)
            except Exception:
                continue
        return templates

    def create_project(self, name: str, template_id: str,
                       variables: Optional[dict] = None) -> dict:
        """创建工程: 复制模板 -> 变量合并 -> 占位符替换 -> 修正 sysroot 相对路径。

        通过后端本地完成 (local 模式直接操作文件系统; ssh 模式用 shell + sftp)。
        """
        if not NAME_RE.match(name):
            return {"ok": False, "error": L("项目名仅限字母/数字/下划线/中划线 (1-64 字符)")}
        tpl_dir = self.conn.join(self._abs("docs/templates"), template_id)
        if not self._exists(self.conn.join(tpl_dir, "template.json")):
            return {"ok": False, "error": L("模板不存在: {template_id}", template_id=template_id)}
        dst = self.conn.join(self.conn.projects_abs, name)
        if self._exists(dst):
            return {"ok": False, "error": L("项目已存在: {name} (可先删除再创建)", name=name)}

        # 读取模板文件清单
        code, out, err = self.executor.exec_sync(
            f"ls -A {shlex.quote(tpl_dir)} 2>/dev/null", timeout=15)
        files = [f.strip() for f in out.splitlines() if f.strip() and f.strip() != "template.json"]

        # 读模板变量
        try:
            import json
            meta = json.loads(self.executor.read_file(
                self.conn.join(tpl_dir, "template.json")))
        except Exception:
            meta = {}
        replacements: Dict[str, str] = {}
        for key, v in (meta.get("variables") or {}).items():
            val = (variables or {}).get(key)
            if val in (None, "", ""):
                val = v.get("default", "")
            replacements[key] = str(val)
            if v.get("type") == "choice" and v.get("choices"):
                idx = next((i for i, c in enumerate(v["choices"])
                            if c.get("value") == str(val)), 0)
                replacements[key + "_SELECT"] = str(idx)

        # 本地模式直接文件操作 (WSL 模式命令在发行版内执行, 本机 os 操作走 UNC);
        # ssh 模式用 sftp + sed
        if self.conn.mode == "local":
            dst_win = self.conn.win_path(dst)
            os.makedirs(dst_win, exist_ok=True)
            for f in files:
                src = self.conn.join(tpl_dir, f)  # WSL 模式为 POSIX 路径 (wsl 内命令用)
                dst_f = self.conn.join(dst, f)
                self.executor.exec_sync(
                    f"cp {shlex.quote(src)} {shlex.quote(dst_f)}", timeout=30)
            for f in files:
                fp = self.conn.join(dst, f)
                fp_win = self.conn.win_path(fp)
                try:
                    content = open(fp_win, "r", encoding="utf-8").read()
                except Exception:
                    continue
                for k, v in replacements.items():
                    content = content.replace("{{" + k + "}}", v)
                if f == "Makefile":
                    # sysroot 相对路径: WSL 用 POSIX relpath, 其余用 os.path
                    if self.conn.is_wsl:
                        sysroot_rel = posixpath.relpath(
                            self._abs("prebuilds/sysroot"), dst)
                    else:
                        sysroot_rel = os.path.relpath(
                            self._abs("prebuilds/sysroot"), dst).replace("\\", "/")
                    content = re.sub(r"^SYSROOT\s*\?=.*$",
                                     f"SYSROOT ?= {sysroot_rel}", content, flags=re.M)
                open(fp_win, "w", encoding="utf-8").write(content)
        else:
            # SSH: 复制 + sed 替换
            mkdir_cmd = f"mkdir -p {shlex.quote(dst)}"
            copy_cmd = " && ".join(
                f"cp {shlex.quote(self.conn.join(tpl_dir, f))} "
                f"{shlex.quote(self.conn.join(dst, f))}"
                for f in files)
            self.executor.exec_sync(f"{mkdir_cmd} && {copy_cmd}", timeout=60)
            for f in files:
                fp = self.conn.join(dst, f)
                for k, v in replacements.items():
                    self.executor.exec_sync(
                        f"sed -i 's|{{{{{k}}}}}|{v}|g' {shlex.quote(fp)}", timeout=30)
                if f == "Makefile":
                    # sysroot 相对路径: 从 projects/<name> 到 prebuilds/sysroot
                    depth = self.conn.project_dir.count("/") + 1
                    sysroot_rel = "/".join([".."] * (depth + 1)) + "/prebuilds/sysroot"
                    self.executor.exec_sync(
                        f"sed -i 's|^SYSROOT ?=.*|SYSROOT ?= {sysroot_rel}|' {shlex.quote(fp)}",
                        timeout=30)

        rel = self.conn.join(self.conn.project_dir, name).replace("\\", "/")
        return {"ok": True, "dir": rel}

    def list_projects(self) -> List[dict]:
        proj_dir = self.conn.projects_abs
        code, out, err = self.executor.exec_sync(
            f"ls -d {shlex.quote(proj_dir)}/*/ 2>/dev/null", timeout=15)
        dirs = [x.strip() for x in out.splitlines() if x.strip()]
        # 合并 hasBinary 检查为一次命令 (避免每项目一次往返)
        has_bin: Dict[str, bool] = {}
        if dirs:
            checks = "; ".join(
                f'[ -e {shlex.quote(self.conn.join(d.rstrip("/"), self.conn.basename(d.rstrip("/"))))} ] '
                f'&& echo "{self.conn.basename(d.rstrip("/"))}=1" '
                f'|| echo "{self.conn.basename(d.rstrip("/"))}=0"'
                for d in dirs)
            _, out2, _ = self.executor.exec_sync(checks, timeout=30)
            for line in out2.splitlines():
                line = line.strip()
                if not line or "=" not in line:
                    continue
                name, _, mark = line.partition("=")
                has_bin[name] = mark == "1"
        projects: List[dict] = []
        for d in dirs:
            base = self.conn.basename(d.rstrip("/"))
            projects.append({
                "name": base,
                "dir": self.conn.join(self.conn.project_dir, base).replace("\\", "/"),
                "hasBinary": has_bin.get(base, False),
            })
        return projects

    def delete_project(self, name: str) -> dict:
        if not NAME_RE.match(name):
            return {"ok": False, "error": L("非法项目名")}
        dst = self.conn.join(self.conn.projects_abs, name)
        if not self._exists(dst):
            return {"ok": False, "error": L("项目不存在: {name}", name=name)}
        self.executor.exec_sync(f"rm -rf {shlex.quote(dst)}", timeout=30)
        return {"ok": True}

    def list_examples(self) -> List[str]:
        ex_dir = self._abs("docs/examples")
        code, out, err = self.executor.exec_sync(
            f"ls -d {shlex.quote(ex_dir)}/*/ 2>/dev/null", timeout=15)
        return [self.conn.basename(d.rstrip("/")) for d in out.splitlines() if d.strip()]

    # ================= Docker 环境 (统一容器编译) =================
    # 编译一律在 Docker 容器内执行:
    #   - 环境一致: SDK 依赖 (apt/python/交叉工具链) 全在镜像里, 宿主机只需提供 docker
    #   - 产物直出: 容器用 -v <sdk>:<sdk> 把 SDK 根挂到容器内相同绝对路径, 编译产物
    #     直接落在宿主真实路径, 烧录工具无需任何传输协议即可读取
    #   - 冲突可控: 容器名对同一 SDK 恒定, 每次编译前先 docker rm -f 清掉残留容器
    # docker_env 里构建的镜像名 (与 SDK 的 docker_env/build.sh 保持一致)
    _DOCKER_IMAGE = "quectel-pi-builder"
    # Yocto 体系专用镜像: 自包含 Dockerfile (见 Pi-H1-Linux-SDK 的 docker_env/Dockerfile)
    _DOCKER_IMAGE_YOCTO = "quectel-pi-yocto-builder"

    def _docker_image(self) -> str:
        """按构建体系选择 docker 镜像名 (yocto 用适配镜像, simple 用原镜像)。"""
        try:
            if self.platform().get("sdk_type") == "yocto":
                return self._DOCKER_IMAGE_YOCTO
        except Exception:  # noqa: BLE001 - 平台识别失败时按 simple 处理
            pass
        return self._DOCKER_IMAGE

    def docker_status(self) -> dict:
        """探测 SDK 所在机器的 Docker 环境状态 (供 Manage Docker 入口使用)。"""
        image = self._docker_image()
        out: dict = {"env_dir": False, "docker_installed": False,
                     "daemon_ok": False, "image_present": False,
                     "image": image, "host_os": self._host_os(), "error": ""}
        try:
            out["env_dir"] = self._exists(self._abs("docker_env/build.sh"))
            pp = self._path_prelude()
            code, o, _ = self.executor.exec_sync(
                pp + "command -v docker >/dev/null 2>&1 && echo YES || echo NO", timeout=15)
            out["docker_installed"] = "YES" in o
            if not out["docker_installed"]:
                return out
            code, o, _ = self.executor.exec_sync(
                pp + "docker info >/dev/null 2>&1 && echo OK || echo NO", timeout=30)
            out["daemon_ok"] = "OK" in o
            # 镜像: 直连不通时尝试免密 sudo (需密码的场景由 /docker/setup 携带密码处理)
            code, o, _ = self.executor.exec_sync(
                pp +
                f"(docker images --format '{{{{.Repository}}}}:{{{{.Tag}}}}' 2>/dev/null "
                f"| grep -q '^{re.escape(image)}:' ) || "
                f"(sudo -n docker images --format '{{{{.Repository}}}}:{{{{.Tag}}}}' 2>/dev/null "
                f"| grep -q '^{re.escape(image)}:' ) && echo IMAGE || echo NOIMAGE",
                timeout=30)
            out["image_present"] = "IMAGE" in o
        except Exception as e:
            out["error"] = str(e)
        return out

    def _host_os(self) -> str:
        """SDK 所在主机 OS 家族: 'linux' / 'darwin' / 其它 (探测结果缓存)。

        决定 docker run 的差异: macOS 走 Docker Desktop (不传 -u, 容器内 root,
        文件属主由 Docker Desktop 自动映射); Linux/WSL 传宿主 uid:gid 保真属主。
        """
        if self._host_os_cached is not None:
            return self._host_os_cached
        fam = "linux"
        try:
            _, out, _ = self.executor.exec_sync("uname -s 2>/dev/null", timeout=15)
            s = (out or "").strip().lower()
            if "darwin" in s:
                fam = "darwin"
            elif "linux" in s:
                fam = "linux"
            elif s:
                fam = s
        except Exception:  # noqa: BLE001 - 探测失败按 linux 处理
            pass
        self._host_os_cached = fam
        return fam

    def _docker_container_name(self) -> str:
        """同一 SDK 恒定的容器名 (编译前据此清理残留容器, 避免并发编译冲突)。"""
        h = hashlib.sha1(self.conn.sdk_abs.encode("utf-8")).hexdigest()[:12]
        return f"qpi-build-{h}"

    def _path_prelude(self) -> str:
        """把插件自管的工具目录 ($HOME/.qpi/bin) 前置到 PATH 的 shell 片段。

        macOS 上没有 Homebrew 时, 插件会把 colima / lima / docker CLI 直接下载到
        $HOME/.qpi/bin (见 docker_setup)。插件经管道执行命令 (非登录 shell), 不会
        读取用户 rc, 因此每次调用 docker/colima 前都要显式补上这个目录。
        """
        return 'export PATH="$HOME/.qpi/bin:$PATH"; '

    def _net_prelude(self) -> str:
        """把统一下载代理 (QPI_DL_PROXY, 由扩展侧注入进程环境) 翻译成 shell 代理变量。

        远端 (SSH) 执行时子进程不会继承本机环境, 因此每条联网命令前都要显式 export;
        git/curl/wget/pip/apt 均读这几个变量。国内镜像主机写入 no_proxy 直连 (镜像
        无需绕代理, 与"代理 + 镜像并用"的策略一致)。未配置代理时返回空串。
        """
        proxy = os.environ.get("QPI_DL_PROXY", "").strip()
        if not proxy:
            return ""
        q = shlex.quote(proxy)
        no_proxy = ",".join((
            "mirrors.tuna.tsinghua.edu.cn", "mirrors.ustc.edu.cn", "mirrors.aliyun.com",
            "mirror.nju.edu.cn", "pypi.tuna.tsinghua.edu.cn", "localhost", "127.0.0.1",
        ))
        nq = shlex.quote(no_proxy)
        prelude = (
            f"export http_proxy={q} https_proxy={q} HTTP_PROXY={q} HTTPS_PROXY={q} "
            f"ALL_PROXY={q} all_proxy={q} no_proxy={nq} NO_PROXY={nq}; "
        )
        # 配了代理时, 扩展侧会把 QPI_GH_PROXY 置空 (走代理不再叠 GitHub 加速前缀);
        # 远端不继承本机环境, 这里同步导出, 让 docker_setup 的前缀选择保持一致。
        if "QPI_GH_PROXY" in os.environ:
            prelude += f"export QPI_GH_PROXY={shlex.quote(os.environ['QPI_GH_PROXY'])}; "
        return prelude

    def _docker_entry(self) -> tuple:
        """探测 docker 调用方式, 返回 (prefix, need_password), 结果缓存。

        - prefix = ""      -> 直接 `docker` (用户在 docker 组 / socket 可访问)
        - prefix = "sudo"  -> 需要 `sudo docker`
        - need_password=True -> sudo 需要密码 (前端应弹窗收集)
        """
        if self._docker_entry_cached is not None:
            return self._docker_entry_cached
        try:
            if self._host_os() == "darwin":
                # macOS 上 docker 从不需要 sudo: Docker Desktop / colima 都把 socket
                # 暴露给当前用户。探测失败时若回退成 `sudo docker`, mac 上必然失败
                # (且 sudo 无 tty 会直接报错), 因此这里直接判定为免 sudo。
                res = ("", False)
            else:
                _, out, _ = self.executor.exec_sync(
                    self._path_prelude() +
                    "docker info >/dev/null 2>&1 && echo DIRECT || "
                    "(sudo -n docker info >/dev/null 2>&1 && echo SUDO || echo PASSWORD)",
                    timeout=40)
                if "DIRECT" in out:
                    res = ("", False)
                elif "SUDO" in out:
                    res = ("sudo", False)
                else:
                    res = ("sudo", True)
        except Exception:  # noqa: BLE001 - 探测失败按最保守 (sudo + 密码) 处理
            res = ("sudo", True)
        self._docker_entry_cached = res
        return res

    def _docker_invalidate(self) -> None:
        """安装/配置 Docker 后清缓存, 使下次重新探测 (用户可能已加入 docker 组)。"""
        self._docker_entry_cached = None

    def _docker_cmd(self) -> str:
        """当前 docker 命令前缀 (含必要的 sudo)。"""
        prefix, _ = self._docker_entry()
        return f"{prefix} docker" if prefix else "docker"

    def _docker_sudo_alias(self, password: str) -> str:
        """构造 bash 前缀: 注入 sudo() 函数, 让命令里多处 `sudo ...` 复用同一份密码。

        有密码: 经 stdin 首行 `read -r QPI_SUDO_PASS`, 用 `sudo -S` 免交互输入
        (SSH 模式下密码不落在命令字符串里, 不会进远端 argv/进程列表)。
        无密码: 用 `sudo -n` 快速失败, 不会挂起等输入。
        """
        if password:
            return (
                "read -r QPI_SUDO_PASS && export QPI_SUDO_PASS && "
                "sudo() { if [ -n \"$QPI_SUDO_PASS\" ]; then "
                "echo \"$QPI_SUDO_PASS\" | command sudo -S -p '' \"$@\"; "
                "else command sudo -n \"$@\"; fi; }; export -f sudo;"
            )
        return "sudo() { command sudo -n \"$@\"; }; export -f sudo;"

    def _docker_run(self, inner: str, env: Optional[Dict[str, str]] = None) -> str:
        """构造可移植的 `docker run` 命令 (前台执行, 日志直接流到任务终端)。

        与 SDK 自带 docker.run 的差异 (故不复用后者):
        - 容器名确定 (qpi-build-<hash>), 便于清理残留/停止;
        - 不挂 /etc/timezone、/opt 等宿主特定路径 (macOS 上不存在);
        - macOS 不传 -u (Docker Desktop 自动映射属主), Linux/WSL 传宿主 uid:gid。
        """
        sdk = self.conn.sdk_abs
        name = self._docker_container_name()
        q = shlex.quote
        host_os = self._host_os()
        head = [self._docker_cmd(), "run", "--rm", "--privileged",
                "--name", q(name),
                "--shm-size", "32G", "--ulimit", "memlock=-1",
                "--tmpfs", "/tmp:exec,rw,size=32G"]
        if host_os != "darwin":
            # 以宿主 uid:gid 运行: 产物属主即宿主用户 (镜像内预置 q1000..q1020 免密 sudo)
            head += ["-u", "\"$(id -u):$(id -g)\""]
        # bind mount SDK 根到容器内相同绝对路径 -> 编译产物直接落在宿主真实路径
        head += ["-v", q(f"{sdk}:{sdk}"), "-w", q(sdk)]
        for k, v in (env or {}).items():
            head += ["-e", q(f"{k}={v}")]
        tail = [q(self._docker_image()), "/bin/bash", "-c", q(inner)]
        # 可选挂载: 宿主的 .ssh / .gitconfig -> 容器内 HOME (git/repo/bitbake 需要);
        # 用 shell 条件判断, 避免宿主不存在时 docker 误建同名目录
        ctn_home = "/root" if host_os == "darwin" else "/home/q$(id -u)"
        mounts = (
            'EXTRA=""; '
            f'if [ -d "$HOME/.ssh" ]; then EXTRA="$EXTRA -v $HOME/.ssh:{ctn_home}/.ssh"; fi; '
            f'if [ -f "$HOME/.gitconfig" ]; then '
            f'EXTRA="$EXTRA -v $HOME/.gitconfig:{ctn_home}/.gitconfig:ro"; fi; '
        )
        return mounts + " ".join(head) + " $EXTRA " + " ".join(tail)

    def _docker_task(self, inner: str, password: str = "",
                     env: Optional[Dict[str, str]] = None,
                     raw: bool = False) -> Task:
        """在容器内 (SDK 根目录) 执行 inner, 返回任务。

        命令结构: [sudo别名] ; docker 预检 ; 清理同名残留容器 ; docker run ...
        - 每次编译前先 `docker rm -f <name>`: 上次编译中途关终端/插件后容器可能仍在
          跑, 直接再启动会两个容器编译同一份文件而冲突 —— 先删再起。
        - kill_hook: 停止按钮 -> 在 SDK 所在主机 docker rm -f (SSH 远端也生效)。
        """
        prefix, _ = self._docker_entry()
        prelude = self._docker_sudo_alias(password) if prefix == "sudo" else ""
        name = self._docker_container_name()
        docker = self._docker_cmd()
        cleanup = f"{docker} rm -f {shlex.quote(name)} >/dev/null 2>&1 || true"
        image = self._docker_image()
        cmd = (
            f"{prelude} "
            f"{self._path_prelude()}"
            f"{{ command -v docker >/dev/null 2>&1; }} || "
            f"{{ echo '[QPi] {L('未检测到 Docker, 请先点击配置构建环境')}'; exit 1; }}; "
            # 镜像缺失时必须就地报错: 否则 docker run 会尝试从 Docker Hub 拉取,
            # 在无法访问官方仓库的网络下只会得到一长串 i/o timeout, 掩盖真正原因。
            f"{{ {docker} image inspect {shlex.quote(image)} >/dev/null 2>&1; }} || "
            f"{{ echo '[QPi] {L('编译镜像缺失, 请先点击「配置构建环境」完成镜像构建')}'; exit 1; }}; "
            f"echo '[QPi] {L('清理残留编译容器')}...'; {cleanup}; "
            f"echo '[QPi] {L('启动 Docker 编译容器')} {name}...'; "
            f"{self._docker_run(inner, env)}"
        )
        task = self.executor.exec(cmd, input_data=password + "\n" if password else None,
                                  raw=raw)
        task.kill_hook = (lambda: self._docker_stop(password))
        return task

    def _docker_stop(self, password: str = "") -> None:
        """强制停止本 SDK 的编译容器 (尽力而为)。

        经 executor 在 SDK 所在主机执行, 因此 local/WSL/SSH 都生效; docker run
        客户端被杀死不会带走容器, 只有 docker rm -f 才能真正停掉容器内编译。
        """
        prefix, _ = self._docker_entry()
        prelude = self._docker_sudo_alias(password) if prefix == "sudo" else ""
        name = self._docker_container_name()
        cmd = (f"{prelude} {self._path_prelude()}{self._docker_cmd()} rm -f "
               f"{shlex.quote(name)} >/dev/null 2>&1 || true")
        try:
            task = self.executor.exec(
                cmd, input_data=password + "\n" if password else None)
            self.executor.drain(task, timeout=20)
        except Exception:  # noqa: BLE001 - 收尾失败只忽略
            pass

    def docker_setup(self, password: str = "") -> Task:
        """安装/配置 Docker 并构建编译镜像 (幂等, 可重复执行)。

        - Linux/WSL: docker 缺失时自动安装 (WSL 用 apt docker.io + systemd; 其他
          Linux 用官方安装器, 回退 apt)。
        - macOS: Docker Desktop 是 GUI 应用无法脚本静默安装, 改为直接下载官方二进制
          (colima + lima + docker CLI + buildx) 到 $HOME/.qpi, 再 `colima start` 起 VM。
          不依赖 Homebrew, 也不需要 sudo。
        - 验证 daemon 可用, 尽力把当前用户加入 docker 组 (减少后续 sudo)。
        - 镜像缺失时用 SDK 的 docker_env/Dockerfile 构建 (该 Dockerfile 自包含)。

        全程流式输出到任务终端, [QPi] 前缀作为阶段提示; sudo 密码经 stdin 注入。
        """
        if not self._exists(self._abs("docker_env/Dockerfile")):
            raise ValueError(
                L("当前 SDK 不包含 docker_env/Dockerfile, 无法使用容器编译"))
        host_os = self._host_os()
        image = self._docker_image()
        env_dir = shlex.quote(self.conn.join(self.conn.sdk_abs, "docker_env"))
        sudo = self._docker_sudo_alias(password)
        if host_os == "darwin":
            # macOS 上 Docker Desktop 是 GUI 应用, 无法脚本静默安装。改用纯 CLI 的
            # colima (轻量 Linux VM) + lima + docker CLI: 三者都有官方二进制, 直接
            # 下载到 $HOME/.qpi 即可, 不依赖 Homebrew, 也不需要 sudo。
            # - colima 只是包装层, 运行时会调用 lima 的 limactl, 故必须一并安装;
            #   lima 压缩包里 bin/ 与 share/ 同级, 因此整包解到 $HOME/.qpi 下。
            # - docker CLI 的 macOS 静态包只有客户端 (daemon 由 colima 的 VM 提供),
            #   且不含 buildx; 而 `docker build` 默认走 BuildKit, 缺 buildx 会直接
            #   报错, 故 buildx 作为 CLI 插件单独安装。
            install = (
                # 架构: uname -m 在 Apple Silicon 为 arm64, Intel 为 x86_64;
                # colima/lima 的资源名与之同名, docker 用 aarch64, buildx 用 amd64。
                'ARCH=$(uname -m); '
                'case "$ARCH" in arm64) DARCH=aarch64; BARCH=arm64;; *) DARCH=x86_64; BARCH=amd64;; esac; '
                'mkdir -p "$HOME/.qpi/bin" "$HOME/.docker/cli-plugins" '
                '|| { echo "[QPi] 无法创建 $HOME/.qpi 目录"; exit 1; }; '
                # 国内直连 GitHub / download.docker.com 很慢甚至反复停 0, 故:
                # - docker CLI 走国内镜像 (清华/中科大/阿里云), 官方源放最后;
                # - GitHub 资源 (colima/lima/buildx) 走加速前缀, 逐个尝试后回退直连。
                #   QPI_GH_PROXY 可显式指定前缀, 置空字符串则强制直连。
                # - 镜像策略 QPI_DL_MIRROR=global/off 时完全不使用国内镜像/加速前缀。
                'MODE="${QPI_DL_MIRROR:-auto}"; '
                'if [ "$MODE" = global ] || [ "$MODE" = off ]; then CN_MIRRORS=""; GH_DEF=""; '
                'else CN_MIRRORS="https://mirrors.tuna.tsinghua.edu.cn/docker-ce '
                'https://mirrors.ustc.edu.cn/docker-ce https://mirrors.aliyun.com/docker-ce"; '
                'GH_DEF="https://gh-proxy.com https://ghfast.top https://ghproxy.net"; fi; '
                'if [ -n "${QPI_GH_PROXY:-}" ]; then GH_LIST="$QPI_GH_PROXY"; '
                'elif [ "${QPI_GH_PROXY+set}" = set ]; then GH_LIST=""; '
                'else GH_LIST="$GH_DEF"; fi; '
                # 限速/卡死保护: 低于 1KB/s 持续 30s 即中断, 换下一个源。
                # -s 抑制 curl 进度条 —— 否则 pty 里每秒几十行进度会把终端刷爆。
                'CURL="curl -fsSL --retry 2 --connect-timeout 15 --speed-limit 1024 --speed-time 30"; '
                'gh_fetch() { for p in $GH_LIST ""; do '
                'if [ -n "$p" ]; then u="$p/$2"; else u="$2"; fi; '
                'echo "[QPi] 下载 $3 <- ${p:-GitHub 直连}"; '
                '$CURL -o "$1" "$u" && return 0; '
                'done; return 1; }; '
                'gh_tag() { for p in $GH_LIST ""; do '
                'if [ -n "$p" ]; then u="$p/https://api.github.com/repos/$1/releases/latest"; '
                'else u="https://api.github.com/repos/$1/releases/latest"; fi; '
                't=$($CURL --max-time 20 "$u" 2>/dev/null '
                "| sed -n 's/.*\"tag_name\": *\"v\\([^\"]*\\)\".*/\\1/p' | head -n 1); "
                '[ -n "$t" ] && { printf \'%s\' "$t"; return 0; }; '
                'done; return 1; }; '
                'docker_fetch() { for m in '
                '$CN_MIRRORS '
                '"https://download.docker.com"; do '
                'echo "[QPi] 下载 docker CLI <- $m"; '
                '$CURL -o "$1" "$m/$2" && return 0; '
                'done; return 1; }; '
                'echo "[QPi] macOS 未检测到 Docker, 开始下载 colima / lima / docker CLI (无需 Homebrew)..."; '
                # colima 资源名不含版本号, 可用 latest 直链
                'gh_fetch "$HOME/.qpi/bin/colima" '
                '"https://github.com/abiosoft/colima/releases/latest/download/colima-Darwin-$ARCH" colima '
                '|| { echo "[QPi] colima 下载失败, 请检查网络后重试"; exit 1; }; '
                'chmod +x "$HOME/.qpi/bin/colima"; '
                # lima 资源名含版本号, 先查最新 tag (查不到则回退固定版本)
                'LIMA_VER=$(gh_tag lima-vm/lima); LIMA_VER=${LIMA_VER:-2.2.1}; '
                'gh_fetch "$HOME/.qpi/lima.tar.gz" '
                '"https://github.com/lima-vm/lima/releases/download/v$LIMA_VER/lima-$LIMA_VER-Darwin-$ARCH.tar.gz" '
                '"lima $LIMA_VER" || { echo "[QPi] lima 下载失败, 请检查网络后重试"; exit 1; }; '
                'tar -C "$HOME/.qpi" -xzf "$HOME/.qpi/lima.tar.gz" && rm -f "$HOME/.qpi/lima.tar.gz" '
                '|| { echo "[QPi] lima 解压失败"; exit 1; }; '
                'docker_fetch "$HOME/.qpi/docker.tgz" '
                '"mac/static/stable/$DARCH/docker-27.5.1.tgz" '
                '|| { echo "[QPi] docker CLI 下载失败, 请检查网络后重试"; exit 1; }; '
                'tar -C "$HOME/.qpi" -xzf "$HOME/.qpi/docker.tgz" '
                '&& mv "$HOME/.qpi/docker/docker" "$HOME/.qpi/bin/docker" '
                '&& chmod +x "$HOME/.qpi/bin/docker" '
                '&& rm -rf "$HOME/.qpi/docker" "$HOME/.qpi/docker.tgz" '
                '|| { echo "[QPi] docker CLI 安装失败"; exit 1; }; '
                # buildx 资源名同样含版本号
                'BX_VER=$(gh_tag docker/buildx); BX_VER=${BX_VER:-0.38.0}; '
                'gh_fetch "$HOME/.docker/cli-plugins/docker-buildx" '
                '"https://github.com/docker/buildx/releases/download/v$BX_VER/buildx-v$BX_VER.darwin-$BARCH" '
                '"buildx $BX_VER" || { echo "[QPi] buildx 下载失败, 请检查网络后重试"; exit 1; }; '
                'chmod +x "$HOME/.docker/cli-plugins/docker-buildx"; '
                'export PATH="$HOME/.qpi/bin:$PATH"; '
                'echo "[QPi] colima / lima / docker CLI / buildx 安装完成."')
            start_daemon = (
                # colima 最新正式版 (v0.10.3) 并没有 --disk-image-mirror / --registry-mirror
                # 参数 (它们只存在于未发布的 main 分支), 故改用 v0.10.3 就支持的做法:
                # 1) VM 磁盘镜像: 自行经加速前缀/代理下载好后用 --disk-image 传入本地文件,
                #    colima 会校验官方 SHA512, 字节一致即通过, 不再联网下载。
                #    镜像 URL 直接从 colima 二进制里提取 (自动匹配版本/架构), 提取失败才
                #    回退到固定版本并加 --force-disk-image 容忍哈希不一致。
                # 2) Docker Hub 加速: 通过 colima 配置模板注入 docker.registry-mirrors
                #    (命令行无对应参数, 这是官方推荐做法)。
                "if command -v colima >/dev/null 2>&1; then "
                "echo '[QPi] 启动 colima 虚拟机 (首次需创建 VM, 可能耗时数分钟)...'; "
                # 加速前缀: 与安装阶段口径一致 (QPI_GH_PROXY 可覆盖/置空, global/off 时禁用)
                "if [ -n \"${QPI_GH_PROXY:-}\" ]; then GH_LIST=\"$QPI_GH_PROXY\"; "
                "elif [ \"${QPI_GH_PROXY+set}\" = set ]; then GH_LIST=\"\"; "
                "else GH_LIST=\"https://gh-proxy.com https://ghfast.top https://ghproxy.net\"; fi; "
                "case \"${QPI_DL_MIRROR:-auto}\" in global|off) GH_LIST=\"\";; esac; "
                "CURL=\"curl -fsSL --retry 2 --connect-timeout 20 --speed-limit 10240 --speed-time 30\"; "
                "case $(uname -m) in arm64) IA=arm64;; *) IA=amd64;; esac; "
                # 从 colima 二进制中提取它自己会使用的镜像 URL (版本自适应)
                "COLIMA_BIN=$(command -v colima); "
                "IMG_URL=$(tr -c '[:print:]\\n' '\\n' < \"$COLIMA_BIN\" 2>/dev/null | grep -o 'https://github.com/abiosoft/colima-core/releases/download/[^[:space:]]*' | grep -- \"-$IA-docker.raw.gz\" | head -n 1); "
                "FORCE=''; "
                "if [ -z \"$IMG_URL\" ]; then "
                "IMG_URL=\"https://github.com/abiosoft/colima-core/releases/download/v0.10.4/ubuntu-24.04-minimal-cloudimg-$IA-docker.raw.gz\"; "
                "FORCE='--force-disk-image'; fi; "
                "IMG_NAME=$(basename \"$IMG_URL\"); IMG_FILE=\"$HOME/.qpi/colima/$IMG_NAME\"; "
                "DISK_OK=''; "
                "if [ -f \"$IMG_FILE\" ]; then DISK_OK=1; "
                "else "
                "mkdir -p \"$HOME/.qpi/colima\"; "
                "for p in $GH_LIST \"\"; do "
                "if [ -n \"$p\" ]; then u=\"$p/$IMG_URL\"; else u=\"$IMG_URL\"; fi; "
                "echo \"[QPi] 下载 colima VM 镜像 <- ${p:-GitHub 直连}\"; "
                "if $CURL -o \"$IMG_FILE.part\" \"$u\"; then mv -f \"$IMG_FILE.part\" \"$IMG_FILE\" && DISK_OK=1; break; fi; "
                "done; fi; "
                "if [ -n \"$DISK_OK\" ]; then echo \"[QPi] 使用本地 VM 镜像: $IMG_FILE\"; set -- --disk-image \"$IMG_FILE\" $FORCE; "
                "else echo '[QPi] VM 镜像预下载失败, 回退 colima 自行下载 (可能较慢)'; set --; fi; "
                # Docker Hub 加速: 逐个探测 /v2/ (200/401 视为可用), 写入 colima 模板
                "REG_CANDIDATES=\"https://docker.m.daocloud.io https://docker.1ms.run https://docker.xuanyuan.me https://docker.1panel.live https://docker.nju.edu.cn\"; "
                "case \"${QPI_DL_MIRROR:-auto}\" in global|off) REG_CANDIDATES=\"\";; esac; "
                "REG_ITEMS=''; REG_LIST=''; "
                "for m in $REG_CANDIDATES; do "
                "c=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 \"$m/v2/\" 2>/dev/null); "
                "if [ \"$c\" = 200 ] || [ \"$c\" = 401 ]; then "
                "REG_ITEMS=\"$REG_ITEMS\n    - $m\"; REG_LIST=\"${REG_LIST:+$REG_LIST, }$m\"; fi; "
                "done; "
                "TPL=\"$HOME/.colima/_templates/default.yaml\"; "
                "if [ -n \"$REG_ITEMS\" ]; then "
                "if [ ! -f \"$TPL\" ]; then "
                # 最小模板: 只显式给出会被 colima 合并过程覆盖的字段; vmType/mountType
                # 留空以便 colima 在 macOS13 上自动选用 vz/virtiofs
                "mkdir -p \"$HOME/.colima/_templates\"; "
                "printf 'arch: host\\nruntime: docker\\nmodelRunner: docker\\ncpuType: host\\nsshConfig: true\\nmounts: []\\nnetwork:\\n  address: false\\n  mode: shared\\n  dnsHosts:\\n    host.docker.internal: host.lima.internal\\ndocker:\\n  registry-mirrors:%s\\n' \"$REG_ITEMS\" > \"$TPL\"; "
                "elif grep -q 'registry-mirrors' \"$TPL\"; then :; "
                "elif grep -q '^docker: {}$' \"$TPL\"; then "
                "awk -v blk=\"docker:\n  registry-mirrors:$REG_ITEMS\" '{ if ($0 == \"docker: {}\") print blk; else print }' \"$TPL\" > \"$TPL.qpi\" && mv \"$TPL.qpi\" \"$TPL\"; "
                "elif grep -q '^docker:' \"$TPL\"; then echo '[QPi] 模板已含自定义 docker 配置, 跳过镜像注入'; "
                "else printf '\\ndocker:\\n  registry-mirrors:%s\\n' \"$REG_ITEMS\" >> \"$TPL\"; fi; "
                "echo \"[QPi] Docker Hub 加速镜像: $REG_LIST\"; "
                "else echo '[QPi] 未找到可用 Docker Hub 加速镜像, 使用官方源'; fi; "
                "colima start --cpu 4 --memory 8 --disk 100 \"$@\" "
                "|| { echo '[QPi] colima 启动失败: 请执行 colima start 查看详情'; exit 1; }; "
                "else "
                "echo '[QPi] Docker daemon 未运行: 请启动 Docker Desktop (或已有的 Docker 服务) 后重试'; "
                "exit 1; "
                "fi")
            add_group = ""
        else:
            start_daemon = (
                "sudo systemctl enable --now docker >/dev/null 2>&1 || "
                "sudo service docker start >/dev/null 2>&1 || true")
            add_group = 'sudo usermod -aG docker "$USER" >/dev/null 2>&1 || true'
            install = (
                "if [ -n \"${WSL_DISTRO_NAME:-}\" ] || grep -qi microsoft /proc/version 2>/dev/null; then "
                "echo '[QPi] WSL 中未检测到 Docker, 安装 WSL 原生 Docker Engine...'; "
                "sudo apt-get update -qq || { echo '[QPi] apt 更新失败, 请检查网络和软件源'; exit 1; }; "
                "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq docker.io || { echo '[QPi] docker.io 安装失败'; exit 1; }; "
                "sudo systemctl enable --now docker || { echo '[QPi] Docker 已安装但无法启动。请在 /etc/wsl.conf 设置 [boot] systemd=true, 执行 wsl --shutdown 后重试'; exit 1; }; "
                "else "
                "echo '[QPi] 未检测到 Docker, 开始下载安装 (首次耗时较长, 请稍候)...'; "
                "if curl -fsSL https://get.docker.com -o /tmp/get-docker.sh && sudo sh /tmp/get-docker.sh; then "
                "echo '[QPi] Docker 安装完成 (get.docker.com).'; "
                "else "
                "echo '[QPi] 官方安装器失败, 回退 apt 安装 docker.io ...'; "
                "sudo apt-get update -qq || true; "
                "sudo apt-get install -y -qq docker.io || { echo '[QPi] apt 安装 docker 失败, 请手动安装 Docker'; exit 1; }; "
                "echo '[QPi] Docker 安装完成 (apt).'; "
                "fi; "
                "fi")
        pull_base = (
            # Docker Hub 在国内经常不可达; 且 colima 默认启用 containerd 镜像存储,
            # 此时 daemon.json 里的 registry-mirrors 对 build 未必生效。稳妥做法是先用
            # 国内加速镜像把 Dockerfile 的基础镜像拉下来并打回官方 tag, 这样
            # `docker build` (默认 --pull=false) 直接命中本地镜像, 完全不再访问 Docker Hub。
            "MODE=\"${QPI_DL_MIRROR:-auto}\"; "
            "if [ \"$MODE\" = global ] || [ \"$MODE\" = off ]; then REG_CANDIDATES=\"\"; "
            "else REG_CANDIDATES=\"https://docker.m.daocloud.io https://docker.1ms.run "
            "https://docker.xuanyuan.me https://docker.1panel.live https://docker.nju.edu.cn\"; fi; "
            "REG_MIRRORS=\"\"; "
            "for m in $REG_CANDIDATES; do "
            "c=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 \"$m/v2/\" 2>/dev/null); "
            "if [ \"$c\" = 200 ] || [ \"$c\" = 401 ]; then REG_MIRRORS=\"$REG_MIRRORS $m\"; fi; done; "
            "if [ -z \"$REG_MIRRORS\" ]; then "
            "echo '[QPi] 未找到可用的 Docker Hub 加速镜像, 基础镜像将从官方源拉取'; "
            "else "
            "for base in $(awk 'tolower($1)==\"from\" { img=$2; if (img ~ /^--/) img=$3; "
            "if (img != \"\" && img != \"scratch\") print img }' "
            + env_dir + "/Dockerfile 2>/dev/null | sort -u); do "
            "$DOCKER image inspect \"$base\" >/dev/null 2>&1 && { echo \"[QPi] 基础镜像 $base 已存在\"; continue; }; "
            "case \"$base\" in */*) repo=\"$base\";; *) repo=\"library/$base\";; esac; "
            "pulled=''; "
            "for m in $REG_MIRRORS; do "
            "echo \"[QPi] 预拉基础镜像 $base <- $m\"; "
            "if $DOCKER pull \"$m/$repo\" >/dev/null; then $DOCKER tag \"$m/$repo\" \"$base\"; pulled=1; break; fi; "
            "done; "
            "[ -n \"$pulled\" ] || echo \"[QPi] 基础镜像 $base 预拉失败, 将回退官方源\"; "
            "done; "
            "fi")
        steps = f"""
echo '[QPi] 检查 Docker 环境...'
if command -v docker >/dev/null 2>&1; then
  echo '[QPi] Docker 已安装.'
else
{install}
fi
if docker info >/dev/null 2>&1; then DOCKER='docker';
elif sudo docker info >/dev/null 2>&1; then DOCKER='sudo docker';
else
  echo '[QPi] 启动 Docker 服务...'
  {start_daemon}
  if docker info >/dev/null 2>&1; then DOCKER='docker';
  elif sudo docker info >/dev/null 2>&1; then DOCKER='sudo docker';
  else echo '[QPi] Docker daemon 不可用: 请启动 docker 服务后重试'; exit 1; fi
fi
{add_group}
echo '[QPi] 检查镜像 {image}...'
if $DOCKER images --format '{{{{.Repository}}}}:{{{{.Tag}}}}' 2>/dev/null | grep -q '^{image}:'; then
  echo '[QPi] 镜像 {image} 已存在, 无需构建.'
else
  echo '[QPi] 镜像 {image} 不存在, 开始构建 (首次需下载基础镜像, 可能耗时数分钟)...'
  {pull_base}
  cd {env_dir}
  $DOCKER build --force-rm -t {image} . || {{ echo '[QPi] 镜像构建失败, 请查看上方日志'; exit 1; }}
  echo '[QPi] 镜像 {image} 构建完成.'
fi
echo '[QPi] Docker 环境配置完成 ✓'
"""
        task = self.executor.exec(
            f"{sudo} {self._path_prelude()}{self._net_prelude()}{steps}",
            input_data=password + "\n" if password else None)
        self._docker_invalidate()
        return task

    # ================= 编译 =================
    def check_sudo(self, mode: str = "all") -> dict:
        """探测该操作在【非交互】执行时是否需要 sudo 密码。

        插件经管道执行命令, sudo 没有 tty 可用来提示密码, 所以必须提前知道
        "要不要问用户密码"。不同场景判定不同:
        - flash: 仅远端设备模式需要 root (qdl/upgrade_tool 访问 USB 需 root)。
        - docker: 配置 Docker (安装 daemon / 构建镜像) 一律要 sudo。
        - 编译模式: 编译一律在 Docker 容器内执行, 门槛是"执行 docker 是否需要
          密码"(用户在 docker 组 / socket 可直连则不需要); 容器内命令免密 sudo,
          故不再看 SDK 的 rootfs 打包脚本是否需要 root。若 docker 本身未安装, 则
          不以"要密码"回应, 而是返回 docker_missing 让前端提示先装/配置 Docker。

        remote=True 表示 SSH 模式: 密码经加密通道 stdin 注入远端 (不落 argv),
        可正常收集; 前端无需再要求免密 sudo, 仅保留 remote 标记供展示/提示。
        """
        remote = self.conn.mode == "ssh"
        # 烧录 (mode="flash"): 仅远端设备模式需要 root。
        # H1 flash.sh 内部裸 sudo (qdl 访问 USB 需 root); M2 远端 upgrade_tool 同理。
        # 本机设备模式 (含 ssh+本机设备拉取后本机烧录) 不需要。
        if mode == "flash":
            if not self.conn.device_is_remote:
                return {"needed": False, "remote": remote,
                        "reason": L("本机烧录不需要 root")}
            try:
                _, out, _ = self.executor.exec_sync(
                    "sudo -n true 2>/dev/null && echo OK || echo NO", timeout=30)
                if "OK" in out:
                    return {"needed": False, "remote": remote,
                            "reason": L("sudo 免密可用")}
            except Exception:
                pass
            return {"needed": True, "remote": remote,
                    "reason": L("远端烧录 (qdl/upgrade_tool) 需要 sudo 权限")}
        # mode="docker": Manage Docker 入口用它探测宿主 sudo 是否免密 (docker: True
        # 让前端识别这是 Docker 配置场景, 而非某个编译模式)。
        if mode == "docker":
            # macOS 走 colima (二进制下载 + 启动均无需 sudo), 不必收集密码。
            if self._host_os() == "darwin":
                return {"needed": False, "remote": remote, "docker": True,
                        "reason": L("macOS 使用 colima, 无需 sudo")}
            try:
                _, out, _ = self.executor.exec_sync(
                    "sudo -n true 2>/dev/null && echo OK || echo NO", timeout=30)
                if "OK" in out:
                    return {"needed": False, "remote": remote, "docker": True,
                            "reason": L("sudo 免密可用")}
            except Exception:
                pass
            return {"needed": True, "remote": remote, "docker": True,
                    "reason": L("配置 Docker 需要 sudo 权限 (安装 docker / 构建镜像)")}
        # 编译模式: 编译在容器内执行, 门槛是执行 docker 是否需要密码。
        # 但若 docker 本身没装, 谈密码没有意义: 直接引导去安装 (macOS 需 Docker
        # Desktop; Linux/WSL 走「配置构建环境」自动安装), 前端据此提示并中止,
        # 不再弹一个和真实原因无关的 sudo 密码框。
        _, out, _ = self.executor.exec_sync(
            self._path_prelude() +
            "command -v docker >/dev/null 2>&1 && echo YES || echo NO", timeout=15)
        if "YES" not in out:
            host_os = self._host_os()
            hint = (L("未检测到 Docker, macOS 请点击「配置构建环境」自动下载并安装 colima "
                      "(无需 Homebrew)")
                    if host_os == "darwin"
                    else L("未检测到 Docker, 请先点击「配置构建环境」安装 Docker"))
            return {"needed": False, "remote": remote, "docker": True,
                    "docker_missing": True, "host_os": host_os, "reason": hint}
        _, need_pwd = self._docker_entry()
        if need_pwd:
            return {"needed": True, "remote": remote, "docker": True,
                    "reason": L("编译在 Docker 容器内执行, 当前用户执行 docker 需要 sudo 密码")}
        return {"needed": False, "remote": remote,
                "reason": L("Docker 可直接访问, 无需密码")}

    def build(self, mode: str = "all", arg: str = "",
              password: str = "", threads: int = 0,
              yocto: Optional[Dict] = None) -> Task:
        """统一编译入口: mode ∈ {all,kernel,boot,overlays,rootfs,app,check,clean}。

        对应 SDK 顶层 build.sh: ./build.sh <mode> [arg]

        password: 执行 docker 需要的 sudo 密码 (见 check_sudo); 编译在容器内进行,
        容器内命令免密 sudo, 因此不再需要它来跑 rootfs 打包。

        编译一律在 Docker 容器内执行 (镜像内预置 SDK 全部依赖, 与宿主环境无关):
        - 产物直出: -v <sdk>:<sdk> 把 SDK 根挂到容器内同绝对路径, 编译产物直接落在
          宿主真实路径, 烧录工具可直接读取 (无需任何传输协议);
        - 冲突可控: 容器名对同一 SDK 恒定, 每次编译前先 docker rm -f 清掉残留容器,
          避免上次编译被中断后残留容器与新容器抢同一份文件;
        - 收尾可控: 任务被停止时经 kill_hook 在 SDK 所在主机 docker rm -f 容器
          (docker run 客户端被杀不会带走容器内的编译进程)。
        """
        # Yocto SDK (SDK_TYPE=yocto): 编译入口是 source quectel_build/compile/build.sh
        # 后的 bash 函数 (buildall/buildkernel/builddtb/...), 与 simple 体系不同。
        if self.platform().get("sdk_type") == "yocto":
            return self._build_yocto(mode, threads, yocto, password)
        if mode == "app":
            if not arg:
                raise ValueError(L("app 编译需要指定应用目录"))
            inner = (f"cd {shlex.quote(self.conn.sdk_abs)} && "
                     f"./build.sh app {shlex.quote(arg)}")
        else:
            inner = f"cd {shlex.quote(self.conn.sdk_abs)} && ./build.sh {shlex.quote(mode)}"
        return self._docker_task(inner, password)

    # ================= Yocto SDK (Pi-H1-Linux-SDK: BitBake 构建体系) =================
    # source quectel_build/compile/build.sh 后的函数名 -> 插件编译模式映射
    _YOCTO_FUNCS = {
        "all": "buildall && buildpackage",
        # "kernel" = 合并编译 Boot + 内核: 一次任务依次执行 builddtb (生成
        # dtb.bin) 与 buildkernel (生成 efi.bin), 侧边栏仅暴露「编译内核」一个按钮
        "kernel": "builddtb && buildkernel",
        # "boot" 保留单步入口 (builddtb), 供脚本/控制台等内部调用; 侧边栏对
        # Yocto SDK 隐藏「编译 Boot」按钮
        "boot": "builddtb",
        "check": "buildenv",
        # "clean" 不在此表: 全量清理在 _build_yocto() 内特判 (需先检查 buildconfig)
    }
    # buildconfig 新签名 (SDK 已拆分两个维度): buildconfig <proj> <rev> <OS> <VER> [SEC] [DEBUG]
    _YOCTO_OS = ("LINUX", "DEBIAN", "UBUNTU")
    _YOCTO_VERSIONS = ("STD", "DBG")
    _YOCTO_FLAGS = ("SEC", "DEBUG")
    # buildconfig 生成的配置头; buildpackage 所需的项目变量需从该文件恢复
    _YOCTO_CFG_HEADER = ("quectel_build/compile/quectel-features-config/"
                         "quectel-buildconfig-gen.h")
    # buildconfig 的第 1 个参数 (arg1=<proj>): 该 BSP 固定项目名 (非 machine 名)
    _YOCTO_CFG_MACHINE = "QSM565DWF"
    # BitBake 工作区目录 (DISTRO=qcom-wayland 决定, 与 setup-environment 一致)
    _YOCTO_BUILD_DIR = "build-qcom-wayland"

    # 并行度上限: 挡住面板手输的离谱数值 (BitBake/make 本身接受任意正整数)
    _YOCTO_THREADS_MAX = 256

    def _yocto_threads_env(self, threads: int = 0) -> str:
        """按用户配置生成并行度环境变量前缀 (threads<=0 时返回空 = 沿用 SDK 默认)。

        为什么用环境变量而不是改 local.conf: BB_NUMBER_THREADS / PARALLEL_MAKE
        都在 poky scripts/oe-buildenv-internal 的 BB_ENV_PASSTHROUGH_ADDITIONS
        白名单里, 环境变量在配置解析前进入 datastore, 因此能覆盖 local.conf 的
        弱赋值 (?=)。而 local.conf 每次 source build.sh 都会被 setup-environment
        整份重建 ("DO NOT EDIT"), 手改必然丢失。

        线程数没有 "必须是 2 的幂" 的限制: BitBake runqueue 只做 int() 转换并拒绝
        <=0, make -jN 同样接受任意正整数。
        """
        try:
            n = int(threads)
        except (TypeError, ValueError):
            n = 0
        if n <= 0:
            return ""
        n = min(n, self._YOCTO_THREADS_MAX)
        return f"export BB_NUMBER_THREADS={n} PARALLEL_MAKE='-j {n} -l {n}'; "

    _YOCTO_BB_STOP = (
        'qpi_kill_tree() { '
        'for c in $(cat /proc/$1/task/$1/children 2>/dev/null); '
        'do qpi_kill_tree "$c"; done; '
        'kill -9 "$1" 2>/dev/null; return 0; }; '
        'qpi_bb_stop() { '
        's=$(head -n1 build-qcom-wayland/bitbake.lock 2>/dev/null); '
        'case "$s" in ""|*[!0-9]*) return 0;; esac; '
        'kill -0 "$s" 2>/dev/null || return 0; '
        'timeout 30 bitbake -m >/dev/null 2>&1; '
        'i=0; while [ $i -lt 20 ]; do kill -0 "$s" 2>/dev/null || return 0; '
        'sleep 0.5; i=$((i+1)); done; '
        '[ "$(head -n1 build-qcom-wayland/bitbake.lock 2>/dev/null)" = "$s" ] '
        '|| return 0; '
        'grep -qa bitbake /proc/$s/cmdline 2>/dev/null || return 0; '
        'qpi_kill_tree "$s"; return 0; }; '
    )

    def _yocto_cmd(self, sdk: str, body: str) -> str:
        """Yocto 编译命令: 在伪终端 (pty) 中执行。

        bitbake (knotty.py) 检测 stdout.isatty() 决定是否渲染进度条:
        - 管道 (非 tty): `if not self.interactive: return`, 进度条被跳过
        - pty: 渲染 `Loading cache: 100% |###|` / `Parsing recipes: 74%` 进度
        用 script -qefc 分配 pty, 使 IDE 终端与系统终端显示一致; 输出由
        executor 按 \\r 断行后, 前端用 \\r 覆盖写还原原地刷新。
        PYTHONUNBUFFERED=1 避免 python 块缓冲导致日志一坨一坨出现。
        进 pty 后先 `stty cols 80 rows 50` + 设 TERM/COLUMNS/LINES: script 的
        pty 没有控制终端可继承尺寸, 默认是 0x0 且 TERM=dumb —— knotty 拿到 0 列
        会把进度条宽度算成 0 (只画成 `8% ||`), 之后交互式输出几乎不再刷新, 终端看
        起来像卡死 (实际 bitbake 一直在编译)。TERM=dumb 还会让 curses 取不到
        `cuu` (光标上移), knotty 直接退回非交互模式 ("Unable to use interactive
        mode for this terminal, using fallback")。
        宽度只给 80 (progressbar 库自己的默认值), 不能给大值: knotty 的 Bar 组件
        (WidgetHFill) 会把进度条**撑满 pty 宽度**, 行长达 term_width。IDE 终端若比
        pty 窄, 这一行就会折行, 折行后 `\\r` 回不到本行行首, 进度条重绘变成一行行
        累积刷屏 (表现为"日志卡在 Loading cache")。80 列对绝大多数终端面板都放得下。

        source build.sh 前后各收尾一次 (见 _YOCTO_BB_STOP):
        - 之前: 清掉上次遗留的 bitbake 常驻 server —— 无论它是已死只剩 socket
          文件 (bitbake 会以为 server 还在, 一直 "No reply from server in 30s
          ... Retrying server connection"), 还是进程仍活着 (它会继承 pty 让
          script 永不退出)。随后才 rm -f socket/lock 文件。
        - 之后: 编译函数(含失败)返回后立刻关闭本次 server, 保证容器能退出并把
          真实退出码带回前端, 而不是永远卡在"运行中"。
        set +e 让编译失败时仍能走到收尾语句 (build.sh 可能打开了 errexit)。
        """
        inner = (f"stty cols 80 rows 50 2>/dev/null; "
                 f"export TERM=xterm-256color COLUMNS=80 LINES=50 "
                 f"PYTHONUNBUFFERED=1; set +e; "
                 f"{self._YOCTO_BB_STOP}"
                 f"qpi_bb_stop; "
                 f"rm -f build-qcom-wayland/bitbake.sock "
                 f"build-qcom-wayland/bitbake.lock; "
                 f"source quectel_build/compile/build.sh >/dev/null 2>&1; "
                 f"set +e; {body}; rc=$?; "
                 f"qpi_bb_stop; "
                 f"exit $rc")
        return (f"cd {sdk} && exec script -qefc {shlex.quote(inner)} /dev/null")

    def _build_yocto(self, mode: str, threads: int = 0,
                     params: Optional[Dict] = None, password: str = "") -> Task:
        """Yocto SDK 编译: **同一会话内** 先 buildconfig 再编译。

        为什么每次都要重跑 buildconfig (SDK README 也是这么写的):
        `source build.sh` 内部无参 `. setup-environment`, 会把 conf/local.conf 与
        conf/auto.conf **整份重建** (文件头写着 "dynamically generated ... DO NOT
        EDIT")。而 buildconfig 的很多效果是靠 source 之后 append/sed 这两个文件实现
        的, 例如:
          - SKIP_DEPLOY_DEBIAN_GNOME_ROOTFS = "1" (LINUX/STD 必需; 否则 do_image
            会去找 prebuild/debian-gnome-rootfs 而报 "prebuild source not found")
          - conf/auto.conf 里的 DEBUG_BUILD / PERFORMANCE_BUILD / SECBOOT_ENABLE
        所以只有在重建**之后**紧跟 buildconfig, 这些设置才有效。SDK 的交互式流程是
        `source -> buildconfig -> buildall`, 天然满足; 插件把面板参数持久化下来,
        编译时于同一会话内复现同一序列 —— 行为与手工执行完全一致, 也避免了
        "先 buildconfig、后 source 时配置被冲掉" 的问题。

        参数来自「配置构建环境」面板保存的配置 (project_id/os/version/sec);
        未保存时明确报错 (buildconfig 是该构建体系的必需步骤)。

        整个流程在 Docker 容器内执行 (见 _docker_task); password 仅用于在 SDK
        所在主机上执行 docker (见 check_sudo), 容器内 BitBake 用户态免密运行。
        """
        if mode in ("app", "overlays", "rootfs"):
            raise ValueError(
                L("Yocto SDK 没有 app/overlays/rootfs 编译入口: "
                "应用请用 BitBake (bitbake <recipe>), "
                "根文件系统由 buildconfig + buildall 统一构建"))

        def _cfg(what: str) -> str:
            """buildconfig 前缀; 未保存配置时给出可操作提示。"""
            if not params:
                raise ValueError(
                    L("尚未保存构建配置, 无法执行{what}。请先点击侧边栏「配置构建环境」填写工程信息 (系统类型 / 项目 ID / 版本) 并保存, 之后编译会自动带上这些参数", what=what))
            args = self._yocto_buildconfig_args(
                params.get("project_id", ""), params.get("os", "LINUX"),
                params.get("version", "STD"),
                params.get("flags") or (["SEC"] if params.get("sec") else None))
            return f"buildconfig {args} && "

        sdk = shlex.quote(self.conn.sdk_abs)
        tenv = self._yocto_threads_env(threads)
        gen = self.conn.join(self.conn.sdk_abs, self._YOCTO_CFG_HEADER)
        # 全量清理 = bitbake cleanall (清目标镜像任务产物) + 删除整个
        # build-qcom-wayland 目录 (BitBake 工作区)。路径基于当前连接的 SDK,
        # 不影响其他 SDK。先 buildconfig 保证 BUILDDIR 就绪 (bitbake 依赖它)。
        if mode == "clean":
            build_dir = self.conn.join(self.conn.sdk_abs, self._YOCTO_BUILD_DIR)
            func = (L("bitbake $TARGET_IMAGE -c cleanall && rm -rf {build_dir} && echo '[QPi] 已删除构建目录 {build_dir}'", build_dir=shlex.quote(build_dir)))
            return self._docker_task(
                self._yocto_cmd(sdk, f"{tenv}{_cfg(L('清理'))}{func}"), password,
                raw=True)
        func = self._YOCTO_FUNCS.get(mode, mode)
        if mode in ("all", "boot", "kernel"):
            what = {"all": L("全量编译"), "boot": L("编译 Boot"),
                    "kernel": L("编译内核 (Boot + 内核)")}[mode]
            cfg = _cfg(what)
            # buildpackage 参数来自环境变量; 从配置头恢复 (与 SDK buildenv 同款解析)。
            # 必须排在 buildconfig **之后** —— gen.h 刚被 buildconfig 重写。
            def _gen_var(name: str) -> str:
                return (f"$(awk '/{name}/{{print $3}}' {shlex.quote(gen)} "
                        f"| sed 's/[\\r\\\"]//g')")
            export = (f"export QUECTEL_PROJECT_NAME={_gen_var('QUECTEL_PROJECT_NAME')} "
                      f"QUECTEL_PROJECT_REV={_gen_var('QUECTEL_PROJECT_REV')} "
                      f"QUECTEL_CUSTOM_NAME={_gen_var('QUECTEL_CUSTOM_NAME')} && ")
            cmd = self._yocto_cmd(sdk, f"{tenv}{cfg}{export}{func}")
            return self._docker_task(cmd, password, raw=True)
        # check 等纯探测入口不需要 buildconfig
        return self._docker_task(self._yocto_cmd(sdk, f"{tenv}{func}"), password,
                                 raw=True)
    def _yocto_buildconfig_args(self, project_id: str = "", os: str = "LINUX",
                                version: str = "STD",
                                flags: List[str] = None) -> str:
        """校验并拼装 buildconfig 的参数 (不含命令名)。

        buildconfig <proj> <rev> <LINUX|DEBIAN|UBUNTU> <STD|DBG> [SEC] [DEBUG]
        - proj:    固定为 _YOCTO_CFG_MACHINE (QSM565DWF, 该 BSP 项目名)
        - rev:     用户填写的项目 ID
        - OS/VER:  两个维度 (DEBIAN/UBUNTU 会自动下载/校验 rootfs 底包)
        """
        if not re.fullmatch(r"[A-Za-z0-9_]{1,64}", project_id or ""):
            raise ValueError(L("项目 ID 仅限字母/数字/下划线, 长度 1-64"))
        os = (os or "LINUX").upper()
        if os not in self._YOCTO_OS:
            raise ValueError(L("系统类型必须是 {opts} 之一", opts='/'.join(self._YOCTO_OS)))
        version = (version or "STD").upper()
        if version not in self._YOCTO_VERSIONS:
            raise ValueError(L("版本必须是 {opts} 之一", opts='/'.join(self._YOCTO_VERSIONS)))
        tokens = [f.upper() for f in (flags or []) if f.upper() in self._YOCTO_FLAGS]
        return " ".join(shlex.quote(x) for x in
                        [self._YOCTO_CFG_MACHINE, project_id, os, version] + tokens)

    def yocto_buildconfig(self, project_id: str = "", os: str = "LINUX",
                          version: str = "STD", flags: List[str] = None) -> Task:
        """Yocto SDK: 配置构建环境 = buildconfig (source 入口后调用)。

        buildconfig <proj> <rev> <LINUX|DEBIAN|UBUNTU> <STD|DBG> [SEC] [DEBUG]
        - proj:    固定为 _YOCTO_CFG_MACHINE (QSM565DWF, 该 BSP 项目名)
        - rev:     用户填写的项目 ID
        - OS/VER:  两个维度, 与 SDK 最新签名一致 (DEBIAN/UBUNTU 会自动下载/
                  校验 rootfs 底包, 断点续传 + 进度标记)

        config_parser.py 依赖 python3 + pyhocon, 由「配置构建环境」的依赖检测补齐。
        """
        args = self._yocto_buildconfig_args(project_id, os, version, flags)
        cmd = (f"cd {shlex.quote(self.conn.sdk_abs)} && "
               f"rm -f build-qcom-wayland/bitbake.sock "
               f"build-qcom-wayland/bitbake.lock && "
               f"source quectel_build/compile/build.sh >/dev/null 2>&1 && "
               f"buildconfig {args}")
        return self.executor.exec(cmd)

    # ---- Yocto 构建环境依赖检测/安装 ----
  
    _YOCTO_APT_DEPS = (
        "gawk wget git diffstat unzip texinfo gcc build-essential chrpath socat "
        "cpio python3 python3-pip python3-pexpect xz-utils debianutils iputils-ping "
        "python3-git python3-jinja2 libegl1 libsdl1.2-dev pylint xterm "
        "python3-subunit mesa-common-dev zstd lz4 locales tar python-is-python3 "
        "file libxml-opml-simplegen-perl vim whiptail g++"
    )
    # 非 apt 安装的命令 (Google git-repo 工具): 官方脚本安装到 /usr/local/bin
    _YOCTO_CMD_DEPS = ("repo",)
    _YOCTO_REPO_URL = "https://storage.googleapis.com/git-repo-downloads/repo"
    # buildconfig 的 config_parser.py / version_parser*.py 直接 `from pyhocon import ...`
    _YOCTO_PY_DEPS = ("pyhocon",)
    # bitbake 需要 en_US.UTF-8 locale, 缺失会报 "unsupported locale setting"
    _YOCTO_LOCALE = "en_US.UTF-8"

    def yocto_env_check(self) -> dict:
        """检测 Yocto SDK 构建环境: apt 系统包 / 命令(repo) / python 模块 / locale。

        缺失项逐条列出 (前端据此提示"自动安装缺失依赖"); 磁盘空间不足也会
        提示 (Yocto 全量编译 sstate+work 常需 50GB+), 但仅提示不阻断。
        返回 {"ok", "missing_apt": [...], "missing_cmd": [...], "missing_py": [...],
              "locale_missing": bool, "need_sudo": bool, "disk_free_gb": float}

        编译已全部走宿主原生, 因此这里始终检测编译机本机依赖 (不再因为 SDK 带
        docker_env 就跳过去依赖容器镜像)。
        """
        missing_apt: List[str] = []
        missing_py: List[str] = []
        locale_missing = False
        apt_ok = False  # 非 apt 发行版 (如 Arch): 无法逐包检测, 视为无需检测
        for pkg in self._YOCTO_APT_DEPS.split():
            code, out, err = self.executor.exec_sync(
                f"dpkg-query -W -f='${{Status}}' {shlex.quote(pkg)} 2>/dev/null",
                timeout=15)
            if "install ok installed" in out:
                apt_ok = True
            else:
                missing_apt.append(pkg)
        if not apt_ok and missing_apt:
            # 一个都没装成/查询失败: 很可能不是 dpkg 系发行版, 交给 SDK 脚本处理
            missing_apt = []
        missing_cmd: List[str] = []
        for cmd in self._YOCTO_CMD_DEPS:
            code, out, err = self.executor.exec_sync(
                f"command -v {cmd} >/dev/null 2>&1 && echo OK || echo NO",
                timeout=15)
            if "OK" not in out:
                missing_cmd.append(cmd)
        for mod in self._YOCTO_PY_DEPS:
            code, out, err = self.executor.exec_sync(
                f"python3 -c 'import {mod}' 2>/dev/null && echo OK || echo NO",
                timeout=15)
            if "OK" not in out:
                missing_py.append(mod)
        code, out, err = self.executor.exec_sync(
            "locale -a 2>/dev/null | grep -qi 'en_US' && echo OK || echo NO",
            timeout=15)
        locale_missing = "OK" not in out
        code, out, err = self.executor.exec_sync(
            "df -k . 2>/dev/null | awk 'NR==2{print $4}'", timeout=15)
        disk_gb = 0.0
        try:
            disk_gb = int(out.strip()) / 1024 / 1024
        except Exception:
            disk_gb = 0.0
        need_sudo = bool(missing_apt or missing_cmd or missing_py or locale_missing)
        return {
            "ok": not need_sudo,
            "missing_apt": missing_apt,
            "missing_cmd": missing_cmd,
            "missing_py": missing_py,
            "locale_missing": locale_missing,
            "need_sudo": need_sudo,
            "disk_free_gb": round(disk_gb, 1),
        }

    def yocto_host_info(self) -> dict:
        """探测构建机资源并给出推荐的并行线程数 (配置面板预填/提示用)。

        线程数不受 "2 的幂" 之类限制 (见 _yocto_threads_env); 这里折算只是避免
        用户填出会把 bitbake server OOM 掉的数字 —— SDK 默认按宿主核数取线程,
        小内存机器上全量编译极易被内核 OOM 杀掉 (终端只报一句含糊的
        "bitbake-server might have died", 实则是 OOM)。

        粗略模型: 每个并发编译任务按 ~1.8GB 峰值内存估算, 给系统留 2GB。
        探测失败返回 suggested_threads=0 (面板则不预填), 不抛异常。
        """
        cpu = mem_mb = swap_mb = 0
        try:
            _, out, _ = self.executor.exec_sync(
                "nproc 2>/dev/null; "
                "awk '/^MemTotal:/{print int($2/1024)}' /proc/meminfo; "
                "awk '/^SwapTotal:/{print int($2/1024)}' /proc/meminfo",
                timeout=30)
            vals = [int(x) for x in out.split() if x.isdigit()]
            if len(vals) >= 3:
                cpu, mem_mb, swap_mb = vals[0], vals[1], vals[2]
        except Exception:  # noqa: BLE001 - 探测失败按"无建议"处理
            pass
        suggested = 0
        if mem_mb > 0:
            suggested = max(1, (mem_mb - 2048) // 1800)
            if cpu > 0:
                suggested = min(suggested, cpu)
        elif cpu > 0:
            suggested = cpu
        suggested = min(suggested, self._YOCTO_THREADS_MAX)
        # SDK README 要求: RAM >= 16GB, swap > 32GB
        low_memory = bool(mem_mb and (mem_mb < 16384 or swap_mb < 32768))
        return {
            "cpu_count": cpu,
            "mem_total_mb": mem_mb,
            "swap_total_mb": swap_mb,
            "suggested_threads": suggested,
            "low_memory": low_memory,
        }

    def yocto_env_setup(self, password: str = "") -> Task:
        """安装 Yocto SDK 构建环境缺失依赖 (apt 包 / repo 命令 / pyhocon / locale)。

        先检测再按缺补装, 避免全量重装; 需要 root, 密码经进程 stdin 传递。
        安装完成后 buildconfig 即可直接运行。
        """
        info = self.yocto_env_check()
        # 每一项失败都记进 $fail (形如 apt:libegl1 / py:pyhocon / cmd:repo),
        # 单项失败不阻断其余项, 最后统一列出需要用户手动安装的东西。
        # 写法注意两点: ① 变量赋值不能放进 ( ) 子壳 (改不到外层), 用 { } 分组;
        # ② 每个步骤都必须以 0 退出 (失败也走 || fail=...), 否则被 && 串起来的
        # 后续步骤会整段跳过, 连最后的汇总都打不出来。
        steps: List[str] = ["fail=''"]
        if info["missing_apt"]:
            pkgs = " ".join(shlex.quote(p) for p in info["missing_apt"])
            # 先整批装 (一次事务解析, 最快); 但 apt 只要有一个包名在该发行版
            # 不存在/已改名就整笔放弃 (exit 100, 一个都不装), 于是后面的
            # python3 -m pip 会因 python3-pip 没装上而报 "No module named pip"
            # 这种与真正原因无关的错。故整批失败后逐个重试: 坏包名只影响它自己。
            steps.append(
                "{ apt-get update -y || fail=\"$fail apt-update\"; } && "
                "{ apt-get install -y " + pkgs +
                " || for p in " + pkgs + "; do apt-get install -y \"$p\" || "
                "fail=\"$fail apt:$p\"; done; }")
        if info["missing_cmd"]:
            for cmd in info["missing_cmd"]:
                if cmd == "repo":
                    # Google 官方 git-repo 脚本: apt 无此包, 直接下载到 /usr/local/bin
                    steps.append(
                        f"{{ (command -v curl >/dev/null 2>&1 && "
                        f"curl -fsSL {self._YOCTO_REPO_URL} -o /usr/local/bin/repo) || "
                        f"(command -v wget >/dev/null 2>&1 && "
                        f"wget -qO /usr/local/bin/repo {self._YOCTO_REPO_URL}); }} "
                        f"&& chmod a+x /usr/local/bin/repo "
                        f"|| fail=\"$fail cmd:{cmd}\"")
        if info["missing_py"]:
            # 系统 python3 可能压根没有 pip 模块 (python3-pip 未装): 直接跑 pip
            # 会报 "No module named pip", 与缺的模块无关, 容易误导排查。
            steps.append("python3 -m pip --version >/dev/null 2>&1 || "
                         "apt-get install -y python3-pip "
                         "|| fail=\"$fail apt:python3-pip\"")
            for mod in info["missing_py"]:
                # Ubuntu 22.04+ 的 pip 在系统 python 下装包会报 externally-managed,
                # 加 --break-system-packages 兜底; 老版本忽略该参数也能跑通
                steps.append(
                    f"python3 -m pip install --break-system-packages {shlex.quote(mod)} "
                    f"2>/dev/null || python3 -m pip install {shlex.quote(mod)} "
                    f"|| fail=\"$fail py:{mod}\"")
        if info["locale_missing"]:
            steps.append(f"locale-gen {self._YOCTO_LOCALE} && "
                         f"update-locale LANG={self._YOCTO_LOCALE} "
                         f"|| fail=\"$fail locale:{self._YOCTO_LOCALE}\"")
        if len(steps) == 1:
            raise ValueError(L("Yocto 构建环境已就绪, 无需安装"))
        # 收尾: 逐项报告没装上的东西 + 对应的人工安装命令; 有失败则整体退出码非 0,
        # 前端显示"依赖安装失败, 详见任务终端输出" (配置本身在前面已保存)。
        steps.append(
            'if [ -n "$fail" ]; then '
            'echo "[QPi] 以下依赖未能自动安装, 请手动安装后重新配置:"; '
            'for f in $fail; do '
            'case " $seen " in *" $f "*) continue ;; esac; seen="$seen $f"; '
            'case "$f" in '
            'apt-update) echo "[QPi]   - 软件源索引更新失败   (手动执行: sudo apt-get update, 修好源后再安装上面列出的软件包)" ;; '
            'apt:*) echo "[QPi]   - 软件包 ${f#apt:}   (手动安装: sudo apt-get install -y ${f#apt:})" ;; '
            'py:*) echo "[QPi]   - Python 模块 ${f#py:}   (手动安装: sudo python3 -m pip install --break-system-packages ${f#py:})" ;; '
            'cmd:*) echo "[QPi]   - 命令 ${f#cmd:}   (请手动安装, 见上方输出)" ;; '
            'locale:*) echo "[QPi]   - 语言环境 ${f#locale:}   (手动安装: sudo locale-gen ${f#locale:})" ;; '
            '*) echo "[QPi]   - $f" ;; '
            'esac; done; echo "[QPi] 其余依赖已安装完成"; false; '
            'else echo "[QPi] 环境依赖安装完成"; fi')
        echo = L("[QPi] 安装 Yocto 构建环境缺失依赖 ...")
        # 整段在 sudo 下执行: 密码只经 stdin 传一次 (sudo -S), 免密环境用 sudo -n
        # 代理变量须在 sudo 内再导出一次 (sudo 默认重置环境), 故并入 inner 脚本。
        inner = self._net_prelude() + " && ".join(steps)
        sudo = "sudo -S" if password else "sudo -n"
        cmd = L("echo '{echo}'; {sudo} bash -c {inner}",
                echo=echo, sudo=sudo, inner=shlex.quote(inner))
        if password:
            return self.executor.exec(cmd, input_data=password + "\n")
        return self.executor.exec(cmd)

    # ---- Yocto 编译产物部署 (push 到设备分区) ----
    # 编译 Boot/内核 的产物是完整分区镜像 (vfat), 部署 = dd 到设备对应分区。
    # 部署目标: part -> (产物文件名, 候选分区标签(按优先级), 展示名)
    #
    # 标签取自 SDK 官方固件 partition_ufs/rawprogram*.xml 里的 label —— 那就是
    # GPT 分区名, 最权威的对照:
    #   efi.bin -> label="efi"    (PHY0; 设备上 by-partlabel/efi   -> /dev/sda1)
    #   dtb.bin -> label="dtb_a"  (PHY4; 设备上 by-partlabel/dtb_a -> /dev/sde2)
    # 备选标签用于兼容其他/早期 SDK 的命名 (Yocto 侧镜像名叫 esp-*, 但设备上的
    # 分区标签是 efi —— 曾因只找 "esp" 而定位失败)。
    _YOCTO_DEPLOY = {
        "boot": ("dtb.bin", ("dtb_a", "dtb"), "DTB"),
        "kernel": ("efi.bin", ("efi", "esp"), "ESP/EFI"),
    }

    def _yocto_target_device(self, labels: tuple) -> str:
        """按候选标签探测设备上目标分区的块设备节点; 找不到返回空串。

        三步 (精确优先, 避免误命中):
          1) /dev/disk/by-partlabel/<label> —— 一次 adb 往返批量查, readlink -f 解析
          2) lsblk PARTLABEL 精确相等
          3) lsblk PARTLABEL 模糊包含 (兜底)

        两个必须避开的坑:
        - 不能用 ls -l /dev/disk/by-partlabel/x | grep -oE '/dev/[a-z0-9]+':
          该输出**自身**含 "/dev/disk", 正则会先命中它, 拿到的是目录而非设备节点。
        - readlink -f 对**不存在**的路径会原样返回路径 (不报错), 所以必须校验
          解析结果不是 by-partlabel 原路径, 且确实是块设备 ([ -b ])。
        """
        adb = self._adb_cmd()
        cands = " ".join(shlex.quote(x) for x in labels)
        probe = (
            f'for l in {cands}; do '
            f'p=$(readlink -f /dev/disk/by-partlabel/$l 2>/dev/null); '
            f'case "$p" in /dev/disk/*) continue;; esac; '
            f'if [ -b "$p" ]; then echo "DEV:$p"; break; fi; '
            f'done')
        code, out, err = self.executor.exec_sync_device(
            f"{adb} shell {shlex.quote(probe)}", timeout=20)
        for line in (out or "").splitlines():
            line = line.strip()
            if line.startswith("DEV:"):
                return line[4:]
        # 2) 精确匹配, 3) 模糊兜底
        code, out, err = self.executor.exec_sync_device(
            f"{adb} shell 'lsblk -rno NAME,PARTLABEL 2>/dev/null'", timeout=15)
        rows = []
        for line in (out or "").splitlines():
            fields = line.split()
            if len(fields) >= 2:
                rows.append((fields[0], fields[1]))
        for want in labels:
            for name, label in rows:
                if label.lower() == want.lower():
                    return "/dev/" + name
        for want in labels:
            for name, label in rows:
                if want.lower() in label.lower():
                    return "/dev/" + name
        return ""


    def deploy_yocto_boot(self, part: str = "both") -> Task:
        """烧录 Yocto 编译产物到设备分区 (仅 Yocto SDK)。

        - boot:   quectel_build/output/dtb.bin -> 设备 dtb 分区
        - kernel: quectel_build/output/efi.bin  -> 设备 esp 分区
        - both:   一次任务依次部署 dtb.bin + efi.bin (侧边栏「烧录内核」)
        产物缺失时明确提示先编译; 自动探测目标分区设备节点, 找不到给出可操作提示。
        """
        part = (part or "both").lower()
        parts = ["boot", "kernel"] if part == "both" else [part]
        for p in parts:
            if p not in self._YOCTO_DEPLOY:
                raise ValueError(L("部署目标必须是 boot/kernel/both 之一 (got {part})", part=part))
        # 前置检查: 所有目标产物都存在 (both 模式任一缺失都明确提示, 避免烧一半)
        for p in parts:
            fname = self._YOCTO_DEPLOY[p][0]
            src = self.conn.join(self.conn.sdk_abs, "quectel_build", "output", fname)
            if not self._exists(src):
                raise ValueError(
                    L("未找到编译产物 {fname} (quectel_build/output/ 下)。请先点击「编译内核」生成 {fname} 后再烧录", fname=fname))
        # 产物在编译机, adb push 需执行位置可读的本地文件:
        # - 远端设备: 产物在编译机, adb 也在编译机, 直接远端 push
        # - 本机设备 (ssh): 先拉取产物到本机临时目录再 push
        import tempfile
        base = os.path.join(tempfile.gettempdir(), "qpi_sdk_deploy")
        os.makedirs(base, exist_ok=True)
        adb = self._adb_cmd()
        steps: List[str] = []
        for p in parts:
            fname, labels, disp = self._YOCTO_DEPLOY[p]
            src = self.conn.join(self.conn.sdk_abs, "quectel_build", "output", fname)
            if self.conn.mode == "ssh" and not self.conn.device_is_remote:
                local = os.path.join(base, fname)
                self.executor.pull(src, local)
                src = local
            if not self.conn.device_is_remote:
                # adb 可能是 Windows 程序 (Windows 宿主 / WSL interop), 源路径要转成
                # Windows 可读形式, 否则 Windows 版 adb 会把 /home/... 当成 C:\home\...
                src = self._adb_local_arg(self.conn.win_path(src))
            dev = self._yocto_target_device(labels)
            if not dev:
                raise ValueError(
                    L("无法自动定位设备上的 {disp} 分区 (候选标签: {labels})。请确认设备已连接 (adb devices), 或手动执行:\n  adb push {src} /tmp/qpi_{fname} && adb shell \"dd if=/tmp/qpi_{fname} of=<{disp}分区设备> bs=4M conv=fsync\"", disp=disp, labels=', '.join(labels), src=src, fname=fname))
            steps.append(L("echo '[QPi] 部署 {fname} -> {dev} ...' && {adb} push {src} /tmp/qpi_{fname} && {adb} shell 'dd if=/tmp/qpi_{fname} of={dev} bs=4M conv=fsync' && {adb} shell 'rm -f /tmp/qpi_{fname}'", fname=fname, dev=dev, adb=adb, src=shlex.quote(src)))
        label = L("内核 (Boot + 内核)") if part == "both" else (
            "Boot" if part == "boot" else L("内核"))
        cmd = (" && ".join(steps) +
               L(" && {adb} shell 'sync' && echo '[QPi] {label} 部署完成, 请重启设备生效'", adb=adb, label=label))
        return self.executor.exec_device(cmd)

    # ================= 构建依赖 (委托给 SDK 自带 setup-deps.sh) =================
    # ---- qemu binfmt (跨架构交叉工具链) ----
    _QEMU_GCC_RE = re.compile(r"^([a-z0-9_]+)-.*-gcc$")

    def _qemu_arches(self) -> List[str]:
        """探测 SDK 中需要 qemu 模拟的交叉工具链架构。

        rootfs 工具链 (toolchains/*/bin/*-gcc) 是 qemu wrapper 脚本: 在 x86_64
        宿主上执行 aarch64 gcc 依赖 binfmt_misc 注册; 原生 ELF 工具链不需要。
        仅 Linux 宿主有意义; 返回如 ["aarch64"]。
        """
        if sys.platform != "linux":
            return []
        tc = self._abs("toolchains")
        # 一次往返: 找出所有 *-gcc 中内容以 "#!" 开头的 (即 wrapper 脚本)
        script = (
            f'for f in {shlex.quote(tc)}/*/bin/*-gcc; do '
            f'[ -f "$f" ] || continue; '
            f'[ "$(head -c 2 "$f" 2>/dev/null)" = "#!" ] && basename "$f"; '
            f'done 2>/dev/null'
        )
        code, out, err = self.executor.exec_sync(script, timeout=30)
        arches: List[str] = []
        for line in out.splitlines():
            m = self._QEMU_GCC_RE.match(line.strip())
            if m and m.group(1) not in arches:
                arches.append(m.group(1))
        return arches

    def check_binfmt(self) -> dict:
        """检测 qemu binfmt 注册状态 (缺失时应用层交叉编译必失败)。

        WSL 未启用 systemd 时 binfmt-support.service 不运行: 包虽已安装,
        内核态未注册, 编译会报 "qemu binfmt is not registered"。
        仅 Linux 宿主且存在 wrapper 工具链时才有意义。
        """
        arches = self._qemu_arches()
        if not arches:
            return {"needed": [], "missing": [], "ok": True, "can_enable": False}
        missing: List[str] = []
        for a in arches:
            code, out, err = self.executor.exec_sync(
                f"grep -q '^enabled$' /proc/sys/fs/binfmt_misc/qemu-{a} "
                f"2>/dev/null && echo ON || echo OFF", timeout=15)
            if "ON" not in out:
                missing.append(a)
        can_enable = False
        if missing:
            code, out, err = self.executor.exec_sync(
                "command -v update-binfmts >/dev/null 2>&1 && echo YES || echo NO",
                timeout=15)
            can_enable = "YES" in out
        return {"needed": arches, "missing": missing,
                "ok": not missing, "can_enable": can_enable}

    def enable_binfmt(self, password: str = "") -> Task:
        """注册 qemu binfmt (需 sudo): sudo update-binfmts --enable qemu-<arch>。

        密码只经进程 stdin 传递, 不进命令行/任务回显。
        """
        info = self.check_binfmt()
        targets = info.get("missing") or []
        if not targets:
            raise ValueError(L("qemu binfmt 已注册, 无需操作"))
        names = [f"qemu-{a}" for a in targets]
        # 有密码: sudo -S 经 stdin 读; 无密码: sudo -n 快速失败 (避免交互挂起)
        sudo = "sudo -S" if password else "sudo -n"
        chain = " && ".join(
            f"{sudo} update-binfmts --enable {shlex.quote(n)}" for n in names)
        cmd = (L("echo '[QPi] 注册跨架构模拟: {names}'; {chain} && echo '[QPi] 完成, 应用层交叉编译现在可用'", names=' '.join(names), chain=chain))
        if password:
            return self.executor.exec(cmd, input_data=password + "\n")
        return self.executor.exec(cmd)

    def check_deps(self) -> dict:
        """探测构建依赖缺失: 调用 SDK 自带 tools/setup-deps.sh check。

        缺失的 apt 包名由 SDK 脚本逐行输出到 stdout; 插件不维护任何依赖清单,
        依赖内容由各 SDK (M1/M2/H1/L1...) 自行决定。SDK 未提供脚本时返回
        {available: False}, 由前端提示更新 SDK。
        额外返回 binfmt (qemu 跨架构注册状态): SDK 脚本只查包, 不查运行态。
        """
        binfmt = self.check_binfmt()
        script = self.conn.join(self.conn.sdk_abs, "tools", "setup-deps.sh")
        code, out, err = self.executor.exec_sync(
            f"test -f {shlex.quote(script)} && echo HAVE || echo NO", timeout=30)
        if "HAVE" not in out:
            return {"available": False, "apt": False, "missing": [], "binfmt": binfmt}
        code, out, err = self.executor.exec_sync(
            "command -v apt-get >/dev/null 2>&1 && echo HAVEAPT || echo NOAPT", timeout=30)
        if "HAVEAPT" not in out:
            return {"available": True, "apt": False, "missing": [], "binfmt": binfmt}
        code, out, err = self.executor.exec_sync(
            f"bash {shlex.quote(script)} check 2>/dev/null || true", timeout=60)
        missing = [ln.strip() for ln in out.splitlines() if ln.strip()]
        return {"available": True, "apt": True, "missing": missing, "binfmt": binfmt}

    def setup_env(self, password: str = "") -> Task:
        """一键配置构建环境: 调用 SDK 自带 build.sh setenv (在 Docker 容器内)。

        流程由 SDK 脚本控制: 系统依赖补齐 + base_image/sysroot 缺失时自动下载/
        提取 + 环境校验。容器内以宿主 uid 运行且免密 sudo, 无需宿主密码。

        QPI_AUTO_FETCH=1: 插件用管道执行命令, stdin 不是终端; H1 等 SDK 的
        buildenv 会据此判定为"无提示环境"而拒绝下载数 GB 固件底包并直接报错退出
        (M2 的 buildenv 没有这道闸门, 所以只有 H1 会失败)。这里显式开启自动下载,
        让各平台行为一致。

        环境配置同样在容器内执行 (与编译同一环境, 避免依赖差异); password 仅用于
        在 SDK 所在主机上执行 docker (见 check_sudo), QPI_AUTO_FETCH 经 -e 注入。
        """
        if self.platform().get("sdk_type") == "yocto":
            raise ValueError(
                L("Yocto SDK 的构建环境配置 = buildconfig 流程: 请点击侧边栏"
                "「配置构建环境」, 按向导填写项目 ID, 选择系统类型 (LINUX/"
                "DEBIAN/UBUNTU) 与版本 (STD/DBG), 插件将自动执行 buildconfig"))
        cmd = f"cd {shlex.quote(self.conn.sdk_abs)} && ./build.sh setenv"
        return self._docker_task(cmd, password, env={"QPI_AUTO_FETCH": "1"})

    def build_chain(self, extra: str) -> str:
        """拼出 source build.sh + extra 的完整命令链 (复用 SDK 环境)。"""
        return (f"cd {shlex.quote(self.conn.sdk_abs)} && "
                f"source ./build.sh >/dev/null 2>&1 && {extra}")

    # ================= 烧录 =================
    def _flash_tools_from_sdk(self, plat: Dict[str, object]) -> List[dict]:
        """Rockchip 平台: SDK 自带宿主机烧录工具 (VERSION.txt 的 FLASH_TOOLS_*)。

        工具随 SDK 分发于 tools/flash/, 插件不再内置也不再联网下载。按「执行烧录的
        平台」选取, 相对路径按 SDK 根解析 —— ssh 模式下即编译机上的路径。未声明或
        文件缺失时返回空, 由调用方回退 PATH 探测。

        执行平台 != 插件宿主平台的情况:
        - 板子接编译机 (device_is_remote): 工具在远端 Ubuntu 上跑, 与宿主无关 -> linux
        - WSL 宿主且板子接本机: 经 interop 跑 Windows 版 (WSL2 无 USB 直通) -> win32
        """
        if self.conn.device_is_remote:
            tool_os = "linux"
        else:
            tool_os = _flash_tool_platform()
        key = {"linux": "linux", "darwin": "mac", "win32": "windows"}.get(tool_os, "")
        if not key:
            return []
        # WSL 宿主经 interop 跑 Windows 版时, 镜像需转 UNC 路径 (远端执行不适用)
        via_interop = not self.conn.device_is_remote and tool_os != sys.platform
        hint_vars = {"board": str(plat.get("board") or "")}
        specs = [("upgrade_tool", "cli", "flash_tools_" + key, "flash.hint.sdkBundled")]
        # 图形工具仅 Windows 提供 (RKDevTool 为 Windows GUI)
        if key == "windows":
            specs.append(("RKDevTool", "gui", "flash_tools_windows_gui",
                          "flash.hint.sdkBundledGui"))
        out: List[dict] = []
        for name, kind, field, hint in specs:
            rel = str(plat.get(field) or "")
            if not rel:
                continue
            p = self._abs(rel)
            if not self._exists(p):
                continue
            entry: dict = {
                "name": name,
                "kind": kind,
                "hint_key": hint,
                "hint_vars": hint_vars,
                "found": True,
                "path": p,
                "sdk_bundled": True,
            }
            if via_interop:
                entry["windows_exe"] = True
            out.append(entry)
        return out

    def _stage_remote_flash_tool(self, remote: str) -> tuple:
        """ssh + 板子接本机: 把编译机上的烧录工具拉到本机临时目录后本地烧录。

        返回 (本机工具路径, 需前置到烧录命令的「拉取 + 赋可执行位」命令)。工具按
        文件名缓存在 <tmp>/qpi_flash_tools/, 已存在则跳过拉取 (命令为空串)。
        """
        import tempfile
        dest_dir = os.path.join(tempfile.gettempdir(), "qpi_flash_tools")
        dest = os.path.join(dest_dir, self.conn.basename(remote) or "upgrade_tool")
        if os.path.isfile(dest) and os.access(dest, os.X_OK):
            return dest, ""
        pull = self._pull_tree_cmd([(remote, dest)])
        prefix = (f"{pull} && chmod +x {shlex.quote(dest)} && "
                  + shlex.quote("echo '[QPi] 已从编译机拉取烧录工具到本机, 开始烧录...'"))
        return dest, prefix

    def flash_tools(self) -> List[dict]:
        """检测烧录工具: SDK 自带 / 本机 PATH / 远端 PATH / 本地目录。

        Rockchip (M2): 优先用 SDK 自带工具 (tools/flash/, 由 VERSION.txt 声明),
        其次本机 PATH 与常见本地目录 (兼容未声明 FLASH_TOOLS_* 的旧 SDK)。
        Qualcomm (H1): 不适用 Rockchip 工具链, 改用 SDK 自带的烧录脚本。
        """
        plat = self.platform()
        if plat.get("family") == "qualcomm":
            return self._flash_tools_sdk_script(plat)
        tools = [
            {"name": "upgrade_tool", "hint_key": "flash.hint.upgradeTool", "hint_vars": {}, "found": False, "path": ""},
            {"name": "rkdeveloptool", "hint_key": "flash.hint.rkdeveloptool", "hint_vars": {}, "found": False, "path": ""},
            {"name": "RKDevTool", "hint_key": "flash.hint.rkDevToolPath", "hint_vars": {}, "found": False, "path": ""},
        ]
        # SDK 自带工具优先; 同名条目就地替换, 避免出现重复选项
        for st in self._flash_tools_from_sdk(plat):
            for i, t in enumerate(tools):
                if t["name"] == st["name"]:
                    tools[i] = st
                    break
            else:
                tools.append(st)
        # ssh 模式: 图形工具是编译机上的 GUI, 既无法在本机弹出也不便整目录拉取, 不列出
        if self.conn.mode == "ssh":
            tools = [t for t in tools if t["name"] != "RKDevTool"]
        if self.conn.device_is_remote:
            # 工具在编译机上执行 (板子也接在编译机): 只探测编译机 PATH
            for t in tools:
                if t.get("found"):
                    continue          # SDK 自带工具已是编译机上的绝对路径
                code, out, err = self.executor.exec_sync(
                    f"command -v {t['name']} 2>/dev/null || echo NOTFOUND", timeout=15)
                p = out.strip()
                if p and p != "NOTFOUND":
                    t["found"] = True
                    t["path"] = p
        else:
            # 本机 PATH (local/ssh 都查本机: 本机装了工具就直接用)
            for t in tools:
                if t.get("found"):
                    continue
                code, out, err = self.executor.exec_sync_local(
                    f"command -v {t['name']} 2>/dev/null || echo NOTFOUND", timeout=15)
                p = out.strip()
                if p and p != "NOTFOUND":
                    t["found"] = True
                    t["path"] = p
            # ssh 模式: 再查编译机 PATH (命中后由 flash_image 拉到本机执行)
            if self.conn.mode == "ssh":
                for t in tools:
                    if t["found"]:
                        continue
                    code, out, err = self.executor.exec_sync(
                        f"command -v {t['name']} 2>/dev/null || echo NOTFOUND", timeout=15)
                    p = out.strip()
                    if p and p != "NOTFOUND":
                        t["found"] = True
                        t["path"] = p
            # 本地目录补充探测 (仅 local 模式, 目录在本机)
            if self.conn.mode == "local":
                for extra in (os.path.expanduser("~/tools/rkdeveloptool"),
                              os.path.expanduser("~/rkbin/tools"),
                              self._abs("tools/pack_tools")):
                    if self._exists(extra):
                        code, out, err = self.executor.exec_sync(
                            f"ls {shlex.quote(extra)} 2>/dev/null", timeout=15)
                        for f in out.splitlines():
                            f = f.strip()
                            if f in ("rkdeveloptool", "upgrade_tool") and \
                                    not any(t["found"] and t["path"].endswith(f) for t in tools):
                                tools.append({"name": f, "hint_key": "flash.hint.localDir", "hint_vars": {},
                                              "found": True, "path": os.path.join(extra, f)})
        # 建立注册表, 供 flash_image 解析工具名 -> 可执行文件
        for t in tools:
            if t.get("found") and t.get("path"):
                self._tool_registry[t["name"]] = t
        return tools

    def _resolve_flash_tool(self, tool: str) -> dict:
        """把前端传入的工具名/路径解析成 {name, kind, found, path}。"""
        if tool in self._tool_registry:
            return self._tool_registry[tool]
        # 直接是可执行文件路径
        if os.path.sep in tool or os.path.exists(tool):
            return {"name": tool, "kind": "cli", "found": True, "path": tool}
        # PATH 探测
        code, out, err = self.executor.exec_sync(
            f"command -v {shlex.quote(tool)} 2>/dev/null || echo NOTFOUND", timeout=15)
        p = out.strip()
        if p and p != "NOTFOUND":
            entry = {"name": tool, "kind": "cli", "found": True, "path": p}
            self._tool_registry[tool] = entry
            return entry
        return {"name": tool, "kind": "cli", "found": False, "path": ""}

    def find_loader(self) -> str:
        """在 prebuilds/base_image 下找 loader 固件 (MiniLoader / RK3576*.bin)。"""
        base = self._abs("prebuilds/base_image")
        code, out, err = self.executor.exec_sync(
            f"find {shlex.quote(base)} -maxdepth 3 -type f \\( -iname '*loader*' "
            f"-o -iname 'rk3576*.bin' -o -iname 'rk3576*.img' \\) 2>/dev/null | head -3",
            timeout=30)
        return out.strip().splitlines()[0] if out.strip() else ""

    def _resolve_image_path(self, image: str) -> str:
        """把前端传入的镜像路径解析成执行位置可用的绝对路径。

        - local 模式: 相对 SDK 根目录或绝对路径
        - ssh 模式: 远端路径 (工具在远端执行时用)
        - ssh 模式 + 本机工具: 先通过 SFTP 拉取到本机临时目录再返回本机路径
        """
        if self.conn.mode == "local":
            img = image if self.conn.isabs(image) else self._abs(image)
            # WSL 模式: img 为发行版内路径, Windows 侧工具 (本机烧录) 需 UNC 路径
            img_win = self.conn.win_path(img)
            if not os.path.exists(img_win):
                raise ValueError(L("镜像不存在: {img}\n请先完成「全量编译」生成 update.img", img=img))
            return img_win
        # ssh 模式: 远端路径
        remote_img = image if self.conn.isabs(image) else self._abs(image)
        if not self._exists(remote_img):
            raise ValueError(L("远端镜像不存在: {img}\n请先完成「全量编译」生成 update.img", img=remote_img))
        return remote_img

    def _pull_cmd(self, remote_img: str) -> tuple:
        """生成「从编译机拉取镜像到本机」的命令 (带实时进度), 返回 (本地路径, 命令)。

        用后端 venv python 内联 SFTP 拉取, 每 5% 输出一行 PULL_PROGRESS:n,
        供前端进度条显示「拉取镜像 n%」。拉完输出 PULL_DONE。
        """
        import tempfile
        cache_dir = os.path.join(tempfile.gettempdir(), "qpi_sdk_flash")
        os.makedirs(cache_dir, exist_ok=True)
        local = os.path.join(cache_dir, os.path.basename(remote_img))
        venv_py = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            ".venv",
            "Scripts" if os.name == "nt" else "bin",
            "python.exe" if os.name == "nt" else "python",
        )
        if not os.path.isfile(venv_py):
            venv_py = sys.executable
        NL = chr(10)
        # 注意: 脚本用 try/except 包裹, stat/get 失败时输出清晰的 PULL_ERROR 中文提示,
        # 而不是裸抛 FileNotFoundError 堆栈 (Windows 下也强制 UTF-8 输出避免乱码)
        py = (
            "def cb(n,t):" + NL +
            "  p=int(n*100.0/t);" + NL +
            "  if p!=last[0] and p%5==0:" + NL +
            "    last[0]=p;print('PULL_PROGRESS:'+str(p),flush=True)" + NL +
            "import paramiko,sys,os" + NL +
            "host,port,user=sys.argv[1],int(sys.argv[2]),sys.argv[3]" + NL +
            "pw=sys.argv[4] or None" + NL +
            "key=os.path.expanduser(sys.argv[5]) if sys.argv[5] else None" + NL +
            "r=sys.argv[6];l=sys.argv[7]" + NL +
            "c=paramiko.SSHClient();c.set_missing_host_key_policy(paramiko.AutoAddPolicy())" + NL +
            "try:" + NL +
            "  c.connect(host,port,user,password=pw,key_filename=key)" + NL +
            "  sftp=c.open_sftp()" + NL +
            "  try:" + NL +
            "    size=sftp.stat(r).st_size;last=[-1]" + NL +
            "    sftp.get(r,l,callback=cb)" + NL +
            "    print('PULL_DONE',flush=True)" + NL +
            "  except IOError:" + NL +
            "    print('PULL_ERROR: 远端镜像不存在或不可读: '+r,flush=True)" + NL +
            "    print('PULL_ERROR: 请先在编译机('+host+')上执行全量编译生成 update.img',flush=True)" + NL +
            "    sys.exit(1)" + NL +
            "  finally:" + NL +
            "    try: sftp.close()" + NL +
            "    except Exception: pass" + NL +
            "    try: c.close()" + NL +
            "    except Exception: pass" + NL +
            "except Exception as e:" + NL +
            "  print('PULL_ERROR: 连接或拉取失败: '+repr(e),flush=True)" + NL +
            "  sys.exit(1)" + NL
        )
        args = (self.conn.host, str(self.conn.port), self.conn.user,
                self.conn.password or "", self.conn.key_path or "",
                remote_img, local)
        cmd = (f"{shlex.quote(venv_py)} -c {shlex.quote(py)} "
               + " ".join(shlex.quote(a) for a in args))
        return local, cmd

    def _find_cli_upgrade_tool(self) -> str:
        """查找可用的命令行 upgrade_tool (替代无命令行模式的图形烧录工具)。

        优先已解析到的 upgrade_tool (SDK 自带 / PATH 探测), 其次图形工具同目录兜底。
        """
        reg = self._tool_registry.get("upgrade_tool")
        if reg and reg.get("found") and reg.get("path"):
            return reg["path"]
        reg = self._tool_registry.get("RKDevTool")
        if reg:
            d = os.path.dirname(reg.get("path", ""))
            cand = os.path.join(d, "upgrade_tool.exe" if os.name == "nt" else "upgrade_tool")
            if os.path.isfile(cand):
                return cand
        return ""

    def flash_image(self, tool: str, image: str,
                    password: str = "") -> Task:
        """全量烧录。tool 可为工具名 / 可执行文件路径。

        平台差异:
        - Qualcomm (H1): 忽略 tool/image, 直接调用 SDK 自带全量烧录脚本
          (password 仅远端设备模式会用到, 见 _flash_via_sdk_script)
        - Rockchip (M2): 用 SDK 自带的宿主机烧录工具 (tools/flash/)

        流程:
        - 解析工具; 图形工具 (RKDevTool) 自动改用命令行 upgrade_tool (不弹 GUI)
        - ssh 模式 + 板子接本机: 工具与镜像都在编译机上, 先带进度拉到本机再本机烧录
        - 本机工具在本机执行 (板子 USB 接本机); 远端工具走远端执行
        """
        plat = self.platform()
        if plat.get("family") == "qualcomm":
            return self._flash_via_sdk_script(plat, password)
        resolved = self._resolve_flash_tool(tool)
        if not resolved.get("found") or not resolved.get("path"):
            raise ValueError(
                L("烧录工具不可用: {tool}\n请确认 SDK 自带烧录工具 (tools/flash/, 由 tools/VERSION.txt 的 FLASH_TOOLS_* 声明) 或系统 PATH 中有该工具", tool=tool))
        # 远端设备模式 (ssh + 板子接编译机): 工具与镜像都在编译机, 全部在编译机上执行
        if self.conn.device_is_remote:
            return self._flash_image_remote(tool, resolved, image)
        exe = resolved["path"]
        note = ""
        if resolved.get("kind") == "gui":
            # 图形工具 (RKDevTool) 无命令行烧录模式: 自动改用命令行 upgrade_tool (不弹 GUI)
            cli = self._find_cli_upgrade_tool()
            if not cli:
                # 找不到命令行工具时退回 GUI
                return self.executor.exec_local(
                    L("{exe} & echo '已启动 {name}, 请在图形界面中完成烧录'", exe=shlex.quote(exe), name=os.path.basename(exe)))
            note = (L("[提示] {exe} 为图形工具, 已自动改用命令行 {cli} 烧录", exe=os.path.basename(exe), cli=os.path.basename(cli)))
            exe = cli
        if self.conn.is_wsl:
            # WSL 模式: SDK 工具在发行版内, Windows 侧要换成 UNC 路径再由本机执行
            # (发行版内解析出的 Linux 路径则留在 WSL 内执行)
            exe = self.conn.win_path(exe)
            is_local_tool = os.path.isfile(exe) or os.path.exists(exe)
        else:
            is_local_tool = os.path.isfile(exe) or self.conn.mode == "local"
        # ssh + 板子接本机: 解析到的工具在编译机上 (SDK 自带或编译机 PATH), 先拉到本机再烧录
        tool_pull = ""
        if self.conn.mode == "ssh" and not is_local_tool:
            exe, tool_pull = self._stage_remote_flash_tool(exe)
            is_local_tool = True

        # 镜像: ssh 模式 + 本机工具 -> 带进度拉取到本机; 其余 -> 直接用镜像路径
        pull_cmd = ""
        if is_local_tool and self.conn.mode == "ssh":
            remote_img = image if self.conn.isabs(image) else self._abs(image)
            if not self._exists(remote_img):
                raise ValueError(L("远端镜像不存在: {img}\n请先完成「全量编译」生成 update.img", img=remote_img))
            # 拉取脚本内部会用 SFTP stat 再次校验 (与拉取同一机制), 不存在时输出友好中文提示
            img_abs, pull_cmd = self._pull_cmd(remote_img)
        else:
            img_abs = self._resolve_image_path(image)
            # WSL 宿主 + Windows 工具 (interop): 镜像需转成 Windows 可访问路径
            # (ext4 -> UNC), Windows 进程才能打开该文件
            if resolved.get("windows_exe"):
                img_abs = _linux_to_win_path(img_abs)

        # WSL 模式 + 本机烧录工具: Windows 工具读不了 \\wsl.localhost\\... UNC 镜像
        # (WSL2 9P 共享对大文件/底层读取不兼容), 任务内先从发行版把镜像复制到
        # 本机临时目录 (WSL 读 ext4 + Windows 读 C: 都稳定), 再让工具读本地文件。
        stage = ""
        if is_local_tool and self.conn.is_wsl:
            import tempfile
            linux_img = self.conn.linux_path(img_abs)
            stage_dir = os.path.join(tempfile.gettempdir(), "qpi_flash")
            os.makedirs(stage_dir, exist_ok=True)
            local_img = os.path.join(stage_dir, os.path.basename(linux_img))
            img_abs = local_img
            stage = (f"wsl.exe -d {shlex.quote(self.conn.wsl_distro)} --cd / -- "
                     f"cp -f {shlex.quote(linux_img)} "
                     f"{shlex.quote(_win_to_wsl_mnt(local_img))} && echo COPY_OK && ")

        base = os.path.basename(exe).lower()
        if "rkdeveloptool" in base:
            loader = self.find_loader()
            if loader:
                cmd = (f"{shlex.quote(exe)} db {shlex.quote(loader)} && "
                       f"sleep 2 && {shlex.quote(exe)} wl 0 {shlex.quote(img_abs)}")
            else:
                cmd = (L("{exe} wl 0 {img} && echo '(未找到 loader, 请确认设备处于 Maskrom 模式)'", exe=shlex.quote(exe), img=shlex.quote(img_abs)))
        elif "upgrade_tool" in base or "upgrade" in base:
            cmd = f"{shlex.quote(exe)} uf {shlex.quote(img_abs)}"
        else:
            raise ValueError(L("不支持的烧录工具: {tool}", tool=tool))

        if tool_pull:
            cmd = f"{tool_pull} && {cmd}"
        if pull_cmd:
            cmd = f"{pull_cmd} && echo PULL_OK && {cmd}"
        if stage:
            cmd = (L("echo '[提示] 正在从 WSL 复制镜像到本机临时目录, 完成后自动烧录...'; {stage}{cmd}", stage=stage, cmd=cmd))
        if note:
            cmd = f"echo {shlex.quote(note)}; {cmd}"
        # 本机工具在本机执行; 远端工具走 executor (ssh 时在远端跑)
        if is_local_tool:
            return self.executor.exec_local(cmd)
        return self.executor.exec(cmd)

    def _flash_image_remote(self, tool: str, resolved: dict,
                            image: str) -> Task:
        """远端设备模式 (ssh + 板子接编译机): 在编译机上用远端工具烧录远端镜像。"""
        exe = self._ensure_remote_flash_tool(tool, resolved)
        img_abs = image if self.conn.isabs(image) else self._abs(image)
        if not self._exists(img_abs):
            raise ValueError(
                L("远端镜像不存在: {img}\n请先完成「全量编译」生成 update.img", img=img_abs))
        base = os.path.basename(exe).lower()
        if "rkdeveloptool" in base:
            loader = self.find_loader()
            if loader:
                cmd = (f"{shlex.quote(exe)} db {shlex.quote(loader)} && "
                       f"sleep 2 && {shlex.quote(exe)} wl 0 {shlex.quote(img_abs)}")
            else:
                cmd = (L("{exe} wl 0 {img} && echo '(未找到 loader, 请确认设备处于 Maskrom 模式)'", exe=shlex.quote(exe), img=shlex.quote(img_abs)))
        elif "upgrade_tool" in base or "upgrade" in base:
            cmd = f"{shlex.quote(exe)} uf {shlex.quote(img_abs)}"
        else:
            raise ValueError(L("不支持的烧录工具: {tool}", tool=tool))
        return self.executor.exec_device(cmd)

    def _ensure_remote_flash_tool(self, tool: str, resolved: dict) -> str:
        """远端设备模式: 返回编译机上可用的烧录工具路径。

        优先用解析到的绝对路径 (SDK 自带工具 tools/flash/ 就在编译机上), 其次查
        编译机 PATH; 都不可用时给出可操作提示。
        """
        exe = (resolved.get("path") or "") if resolved.get("found") else ""
        if exe:
            check = f"test -x {shlex.quote(exe)} && echo OK || echo NO"
            code, out, err = self.executor.exec_sync(check, timeout=15)
            if "OK" not in out:
                # SDK 经 zip 分发/解压时可能丢失可执行位 (Windows 上常见), 补一次再判
                code, out, err = self.executor.exec_sync(
                    f"chmod +x {shlex.quote(exe)} >/dev/null 2>&1; {check}", timeout=15)
            if "OK" in out:
                return exe
        code, out, err = self.executor.exec_sync(
            f"command -v {shlex.quote(tool)} 2>/dev/null || echo NOTFOUND", timeout=15)
        p = out.strip()
        if p and p != "NOTFOUND":
            return p
        raise ValueError(
            L("远端编译机没有可用的烧录工具: {tool}\n"
              "请确认 SDK 已随包分发烧录工具 (tools/flash/, 由 tools/VERSION.txt 的 FLASH_TOOLS_* 声明), "
              "或在编译机安装 (如 sudo apt install rkdeveloptool) 并加入 PATH", tool=tool))

    # upgrade_tool 的 DownloadImage(DI) 分区参数: DI <-p|-b|-k|-s|-r|-m|-u|-t|-re image>
    _PARTITION_ARG: Dict[str, str] = {
        "parameter": "-p", "boot": "-b", "kernel": "-k", "system": "-s",
        "recovery": "-r", "misc": "-m", "uboot": "-u", "trust": "-t",
        "resource": "-re",
    }

    def _partition_lba(self, exe: str, partition: str) -> str:
        """用 PartitionList(PL) 查分区起始扇区 (供 rkdeveloptool 等无 DI 的情况)。

        PL 各行列首可能是十进制序号 (如 "0 0x4000 0x8000 boot"), 直接取第一个数字
        字段会把序号误当 LBA。因此优先取行内十六进制字段 (LBA 通常为 hex);
        没有 hex 时取分区名之后的第一个十进制数。
        """
        code, out, err = self.executor.exec_sync_device(
            f"{shlex.quote(exe)} PL", timeout=30)
        text = (out or "") + "\n" + (err or "")
        for line in text.splitlines():
            parts = line.split()
            if len(parts) < 2:
                continue
            if not any(p.lower() == partition.lower() for p in parts):
                continue
            hexes = [p for p in parts if re.fullmatch(r"0[xX][0-9a-fA-F]+", p)]
            if hexes:
                return hexes[0]
            idx = next(i for i, p in enumerate(parts)
                       if p.lower() == partition.lower())
            for p in parts[idx + 1:]:
                if re.fullmatch(r"\d+", p):
                    return p
        return ""

    def flash_partition(self, tool: str, partition: str, image: str = "") -> Task:
        """烧录单个分区 (Rockchip)。image 留空时使用 build/result/<partition>.img。

        - upgrade_tool: 用 DownloadImage  (DI <分区参数> <镜像>)
        - 其他工具 (如 rkdeveloptool): 先 PartitionList(PL) 查起始扇区, 再 WriteLBA(WL)
        - Qualcomm (H1): SDK 脚本只支持整盘烧录, 不支持单分区
        """
        plat = self.platform()
        if plat.get("family") == "qualcomm":
            raise ValueError(
                L("{board} SDK 只支持整盘烧录, 请使用「全量烧录」", board=plat.get('board') or L('该')))
        if not partition:
            raise ValueError(L("请指定要烧录的分区名"))
        image = image or f"build/result/{partition}.img"

        resolved = self._resolve_flash_tool(tool)
        if not resolved.get("found") or not resolved.get("path"):
            raise ValueError(
                L("烧录工具不可用: {tool}\n请确认 SDK 自带烧录工具 (tools/flash/, 由 tools/VERSION.txt 的 FLASH_TOOLS_* 声明) 或系统 PATH 中有该工具", tool=tool))
        # 远端设备模式 (ssh + 板子接编译机): 工具/镜像都在编译机, 在编译机上执行
        if self.conn.device_is_remote:
            return self._flash_partition_remote(tool, resolved, partition, image)
        exe = resolved["path"]
        note = ""
        if resolved.get("kind") == "gui":
            cli = self._find_cli_upgrade_tool()
            if not cli:
                raise ValueError(
                    L("图形烧录工具不支持单分区烧录, 请在图形界面中手动操作"))
            note = (L("[提示] {exe} 为图形工具, 已自动改用命令行 {cli} 烧录", exe=os.path.basename(exe), cli=os.path.basename(cli)))
            exe = cli

        base = os.path.basename(exe).lower()
        is_rkdev = "rkdeveloptool" in base
        arg = self._PARTITION_ARG.get(partition.lower())
        if not is_rkdev and not arg:
            raise ValueError(
                L("分区名不受支持: {partition}\nupgrade_tool 支持: {supported}", partition=partition, supported=', '.join(self._PARTITION_ARG)))

        # 镜像: 相对 SDK 根目录或绝对路径 (ssh 模式下为编译机上的路径)
        img = image if self.conn.isabs(image) else self._abs(image)
        if not self._exists(img):
            raise ValueError(
                L("镜像不存在: {img}\n请先编译生成 {name}", img=img, name=os.path.basename(img)))
        # ssh + 板子接本机: 工具与镜像都在编译机上, 都要先拉到本机
        tool_remote = self.conn.mode == "ssh" and not os.path.isfile(exe)
        tool_pull = ""
        img_pull = ""
        if tool_remote:
            exe, tool_pull = self._stage_remote_flash_tool(exe)
            img_abs, img_pull = self._pull_cmd(img)
        else:
            img_abs = self.conn.win_path(img)          # Windows 宿主 + WSL 路径 -> UNC
            if resolved.get("windows_exe") and not img_abs.startswith("\\\\") \
                    and not re.match(r"^[A-Za-z]:", img_abs):
                img_abs = _linux_to_win_path(img_abs)  # WSL 宿主 + Windows 工具 -> UNC

        if is_rkdev:
            lba = self._partition_lba(exe, partition)
            if not lba:
                raise ValueError(
                    L("未能从分区表找到分区: {partition}\n请确认设备已进入 Maskrom/Loader 模式, 或改用「全量烧录」", partition=partition))
            cmd = f"{shlex.quote(exe)} wl {shlex.quote(lba)} {shlex.quote(img_abs)}"
        else:
            cmd = f"{shlex.quote(exe)} DI {arg} {shlex.quote(img_abs)}"
        if img_pull:
            cmd = f"{img_pull} && echo PULL_OK && {cmd}"
        if tool_pull:
            cmd = f"{tool_pull} && {cmd}"
        if note:
            cmd = f"echo {shlex.quote(note)}; {cmd}"
        return self.executor.exec_device(cmd)

    def _flash_partition_remote(self, tool: str, resolved: dict,
                                partition: str, image: str) -> Task:
        """远端设备模式: 在编译机上用远端工具烧录远端分区镜像。"""
        exe = self._ensure_remote_flash_tool(tool, resolved)
        base = os.path.basename(exe).lower()
        is_rkdev = "rkdeveloptool" in base
        arg = self._PARTITION_ARG.get(partition.lower())
        if not is_rkdev and not arg:
            raise ValueError(
                L("分区名不受支持: {partition}\nupgrade_tool 支持: {supported}", partition=partition, supported=', '.join(self._PARTITION_ARG)))
        img = image if self.conn.isabs(image) else self._abs(image)
        if not self._exists(img):
            raise ValueError(
                L("远端镜像不存在: {img}\n请先编译生成 {name}", img=img, name=os.path.basename(img)))
        if is_rkdev:
            lba = self._partition_lba(exe, partition)
            if not lba:
                raise ValueError(
                    L("未能从分区表找到分区: {partition}\n请确认设备已进入 Maskrom/Loader 模式, 或改用「全量烧录」", partition=partition))
            cmd = f"{shlex.quote(exe)} wl {shlex.quote(lba)} {shlex.quote(img)}"
        else:
            cmd = f"{shlex.quote(exe)} DI {arg} {shlex.quote(img)}"
        return self.executor.exec_device(cmd)


    # ================= 设备调试 (adb) =================
    # 插件唯一内置的可执行文件: Windows adb (含 2 个伴随 DLL)。板子 USB 接 Windows 时
    # 必须有 Windows 版 adb, 而用户机器通常没装 platform-tools, 故随插件分发一份。
    _BUNDLED_ADB: Dict[str, str] = {
        "win32": "windows/adb/adb.exe",
        # linux/mac 暂无内置 adb (平台自装); 后续可补充
    }

    _ADB_APT_PKG = "android-tools-adb"

    # ---- 本机 adb 解析 (拆成小步, 便于按宿主平台组合与单测) ----

    def invalidate_adb_cache(self) -> None:
        """清空本机 adb 解析缓存 (连接变更/装好 adb 后调用, 让下次重新解析)。"""
        self._adb_exe_cached = None
        self._adb_is_windows = False

    def _adb_local_path(self) -> str:
        """本机 PATH 中的 adb (exec_sync_local); 命中则写缓存并判定是否 Windows 程序。"""
        try:
            code, out, err = self.executor.exec_sync_local(
                "command -v adb 2>/dev/null || echo NOTFOUND", timeout=15)
            p = out.strip()
            if p and p != "NOTFOUND":
                self._adb_exe_cached = p
                # PATH 上的 .exe / /mnt/c 路径也是 Windows 程序
                self._adb_is_windows = p.lower().endswith(".exe") or p.startswith("/mnt/")
                return p
        except Exception:
            pass
        return ""

    def _adb_win_via_where(self) -> str:
        """WSL 宿主下经 interop 查 Windows PATH 里的 adb.exe (platform-tools)。

        板子 USB 接在 Windows 侧, Windows 的 adb 才看得到设备, 故优先。
        输出可能多行 / 带引号 / CRLF / 含未挂载盘符 (如 D:\\...), 逐行清理后
        必须确认 .exe 且文件在 WSL 里可见才采用。
        """
        try:
            code, out, err = self.executor.exec_sync_local(
                "where.exe adb 2>/dev/null || echo NOTFOUND", timeout=15)
            for line in out.splitlines():
                win = line.strip().strip('"').rstrip("\r")
                if not win or win == "NOTFOUND" or not win.lower().endswith(".exe"):
                    continue
                cand = _win_to_wsl_path(win)
                if os.path.isfile(cand):
                    self._adb_exe_cached = cand
                    self._adb_is_windows = True
                    return cand
        except Exception:
            pass
        return ""

    def _adb_stage_bundled_win(self, src: str) -> str:
        r"""把内置 Windows adb 落到 Windows 本地盘, 返回落地后的 adb.exe 路径。

        为什么必须落地: adb.exe 依赖同目录的 AdbWinApi.dll, 而 Windows 拒绝从网络
        路径 (\\wsl.localhost\...) 加载 DLL —— 直接从 WSL 文件系统启动会以
        STATUS_DLL_NOT_FOUND (0xC0000135, 经 interop 表现为 exit=53) 静默失败。
        复制到 C:\... 后再启动即正常, 且**不依赖 Windows PATH 里是否装了 adb**。

        落地目录: %LOCALAPPDATA%\QuectelPi\adb\ ; 已存在且大小一致则跳过复制。
        任何一步失败都返回空串 (由调用方回退到 PATH 里的 adb)。
        """
        import shutil
        try:
            _, out, _ = self.executor.exec_sync_local(
                "cmd.exe /c echo %LOCALAPPDATA% 2>/dev/null", timeout=15)
            win_dir = out.strip().splitlines()[0].strip() if out.strip() else ""
            if not win_dir or "\\" not in win_dir:
                return ""
            dest_dir = os.path.join(_win_to_wsl_path(win_dir), "QuectelPi", "adb")
            os.makedirs(dest_dir, exist_ok=True)
            src_dir = os.path.dirname(src)
            for name in ("adb.exe", "AdbWinApi.dll", "AdbWinUsbApi.dll"):
                s = os.path.join(src_dir, name)
                d = os.path.join(dest_dir, name)
                if not os.path.isfile(s):
                    continue
                if os.path.isfile(d) and os.path.getsize(d) == os.path.getsize(s):
                    continue
                shutil.copy2(s, d)
            dest = os.path.join(dest_dir, "adb.exe")
            if os.path.isfile(dest):
                os.chmod(dest, 0o755)
                return dest
        except Exception:
            pass
        return ""

    def _adb_bundled_win(self) -> str:
        """插件内置的 Windows adb.exe (tools/windows/adb/)。

        仅当能执行 Windows 程序时可用: Windows 宿主, 或 WSL 宿主且 interop 已启用。
        原生 Linux / macOS 返回空串 (内置的是 .exe, 跑不起来)。
        WSL 宿主下先把 exe 与伴随 DLL 复制到 Windows 本地盘再返回 (见
        _adb_stage_bundled_win), 否则 Windows 无法加载其 DLL。
        """
        if not (sys.platform == "win32" or (_is_wsl_host() and _wsl_interop_ok())):
            return ""
        rel = self._BUNDLED_ADB.get("win32")
        if not rel:
            return ""
        p = os.path.join(_bundled_tools_dir(), rel)
        if not os.path.isfile(p):
            return ""
        if _is_wsl_host():
            p = self._adb_stage_bundled_win(p)
            if not p:
                return ""
        self._adb_exe_cached = p
        self._adb_is_windows = True
        return p

    def _adb_exe(self) -> str:
        """解析本机可用的 adb 可执行文件 (按宿主平台; 远端设备走编译机)。

        - WSL + interop 可用: Windows PATH 的 adb.exe -> 内置 adb.exe -> WSL PATH 的 adb
          (板子 USB 接在 Windows 上, WSL2 无 USB 直通, Windows 的 adb 才看得到设备)
        - 其余宿主 (Windows / 原生 Linux / macOS): 本机 PATH -> 内置 (仅 Windows)
        解析失败 **不写缓存**, 保留后续重试机会 (装好 adb 无需重启后端)。
        """
        if self.conn.device_is_remote:
            return self._adb_exe_remote()
        if self._adb_exe_cached is not None:
            return self._adb_exe_cached
        if _is_wsl_host() and _wsl_interop_ok():
            return self._adb_win_via_where() or self._adb_bundled_win() or self._adb_local_path()
        return self._adb_local_path() or self._adb_bundled_win()

    def _adb_local_arg(self, path: str) -> str:
        """本机路径 -> 当前 adb 能读写的路径 (供 push/pull 的本机侧参数)。

        adb 是 Windows 程序时 (Windows 宿主 / WSL interop), Windows 版 adb 会把
        POSIX 路径 (如 /home/x) 当成"当前盘根目录"下的路径而失败, 必须先转成 Windows
        可访问形式: /mnt/c/... -> C:\\..., 其余 -> \\\\wsl.localhost\\<发行版>\\...
        远端设备时 adb 与路径都在编译机上, 不做转换。
        """
        if self.conn.device_is_remote or not self._adb_is_windows:
            return path
        if path.startswith("\\\\") or re.match(r"^[A-Za-z]:", path):
            return path
        return _linux_to_win_path(path)

    def _adb_exe_remote(self) -> str:
        """解析远端可用的 adb (仅 device_location=remote)。

        远端为 Linux 编译机: 插件无内置 Linux adb, 只查远端 PATH。
        """
        try:
            code, out, err = self.executor.exec_sync(
                "command -v adb 2>/dev/null || echo NOTFOUND", timeout=15)
            p = out.strip()
            if p and p != "NOTFOUND":
                self._adb_exe_cached = p
                self._adb_is_windows = False
                return p
        except Exception:
            pass
        self._adb_exe_cached = ""
        return ""

    def _adb_cmd(self) -> str:
        adb = self._adb_exe()
        if not adb:
            if self.conn.device_is_remote:
                hint = (L("远端编译机未找到 adb 命令。\n"
                        "请在远端安装 platform-tools 并把 adb 加入 PATH, 例如:\n"
                        "  sudo apt install android-tools-adb"))
            elif _is_wsl_host():
                hint = (L("WSL 下已尝试 Windows 侧 platform-tools (where adb) / 插件内置 / WSL PATH 中的 adb。\n"
                        "板子 USB 接在 Windows 上, 请安装 Windows platform-tools (含 adb.exe 且在 Windows PATH 中) 后重试; "
                        "或安装 Linux adb 并用 usbipd-win 把 USB 直通进 WSL。"))
            elif sys.platform == "darwin":
                hint = (L("macOS 下未找到 adb, 请安装 platform-tools:\n"
                        "  brew install --cask android-platform-tools"))
            elif sys.platform.startswith("linux"):
                hint = (L("本机未找到 adb, 请安装 platform-tools:\n"
                        "  sudo apt-get install -y android-tools-adb"))
            else:
                hint = (L("Windows 下可使用插件内置 adb: "
                        "tools/windows/adb/adb.exe\n"
                        "或安装 platform-tools 并把 adb 加入 PATH"))
            raise ValueError(L("未找到 adb 命令 (本机无可用 adb)\n") + hint)
        return shlex.quote(adb)

    def _sudo_passwordless(self) -> bool:
        """sudo -n true 是否可用 (免密配置或凭据已缓存)。"""
        try:
            _, out, _ = self.executor.exec_sync_local(
                "sudo -n true 2>/dev/null && echo OK || echo NO", timeout=30)
            return "OK" in out
        except Exception:
            return False

    def adb_env_check(self) -> dict:
        """检测本机 adb 环境 (供前端决定是否自动安装)。

        installable=True 仅「本机设备 + 原生 Linux + 缺 adb」; 其余情况给出 reason,
        前端不代为安装 (Windows/WSL 有内置 adb.exe 兜底; macOS 用 brew;
        远端设备用编译机自己的 adb)。
        """
        host = _adb_host_kind()
        path = self._adb_exe()
        result = {
            "found": bool(path),
            "path": path,
            "host": host,
            "is_windows": self._adb_is_windows,
            "installable": False,
            "pkg": self._ADB_APT_PKG,
            "need_sudo": False,
            "reason": "",
        }
        if result["found"]:
            result["reason"] = L("已找到 adb, 无需安装")
            return result
        if self.conn.device_is_remote:
            result["reason"] = L("远端设备: adb 需装在被连接的编译机上")
            return result
        if host == "linux":
            result["installable"] = True
            result["need_sudo"] = not self._sudo_passwordless()
            result["reason"] = L("本机缺少 adb, 可自动安装")
            return result
        if host == "macos":
            result["reason"] = L("macOS 请手动安装: brew install --cask android-platform-tools")
            return result
        if host == "wsl":
            result["reason"] = L("WSL 下未找到 Windows 侧 adb, 请安装 Windows platform-tools")
        else:
            result["reason"] = L("未找到 adb, 请安装 platform-tools")
        return result

    def adb_setup(self, password: str = "") -> Task:
        """本机 (原生 Linux) 自动安装 adb (android-tools-adb)。

        仅在 adb_env_check() 判定 installable 时由前端调用; 需要 root,
        密码经进程 stdin 传递 (sudo -S), 免密环境用 sudo -n。
        """
        if self.conn.device_is_remote:
            raise ValueError(L("远端设备的 adb 请在编译机上安装, 插件不代为安装"))
        host = _adb_host_kind()
        if host != "linux":
            raise ValueError(L("当前宿主 ({host}) 不支持自动安装 adb, 请手动安装", host=host))
        if self._adb_exe():
            raise ValueError(L("本机已存在 adb, 无需安装"))
        pkg = self._ADB_APT_PKG
        # 写法同 yocto_env_setup: apt-get update 失败不阻断; 安装失败记录并以非 0 退出,
        # 末尾统一列出人工安装命令。
        steps: List[str] = ["fail=''"]
        steps.append('{ apt-get update -y || fail="$fail apt-update"; }')
        steps.append(
            "{ apt-get install -y " + shlex.quote(pkg) +
            " || fail=\"$fail apt:" + pkg + "\"; }")
        steps.append(
            'if [ -n "$fail" ]; then '
            'echo "[QPi] 以下依赖未能自动安装, 请手动安装后重试:"; '
            'for f in $fail; do '
            'case "$f" in '
            'apt-update) echo "[QPi]   - 软件源索引更新失败   (手动执行: sudo apt-get update, 修好源后再安装)" ;; '
            'apt:*) echo "[QPi]   - 软件包 ${f#apt:}   (手动安装: sudo apt-get install -y ${f#apt:})" ;; '
            '*) echo "[QPi]   - $f" ;; '
            'esac; done; false; '
            'else echo "[QPi] adb 安装完成"; fi')
        echo = L("[QPi] 正在安装 adb ({pkg}) ...", pkg=pkg)
        # 代理变量须在 sudo 内再导出一次 (sudo 默认重置环境), 故并入 inner 脚本。
        inner = self._net_prelude() + " && ".join(steps)
        sudo = "sudo -S" if password else "sudo -n"
        cmd = L("echo '{echo}'; {sudo} bash -c {inner}",
                echo=echo, sudo=sudo, inner=shlex.quote(inner))
        if password:
            return self.executor.exec(cmd, input_data=password + "\n")
        return self.executor.exec(cmd)

    def adb(self, args: str) -> Task:
        """执行 adb 命令 (按「设备连接位置」: 本机或远端编译机执行)。"""
        return self.executor.exec_device(f"{self._adb_cmd()} {args}")

    def adb_devices(self) -> str:
        try:
            code, out, err = self.executor.exec_sync_device(
                f"{self._adb_cmd()} devices 2>&1", timeout=20)
            return out or err
        except Exception as e:
            return str(e)

    def adb_devices_parsed(self) -> List[dict]:
        raw = self.adb_devices()
        devices = []
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("*") or "List of devices" in line or "attached" in line:
                continue
            parts = line.split()
            if len(parts) >= 2:
                devices.append({"serial": parts[0], "state": parts[1]})
        return devices

    def deploy(self, source: str, target: str, kind: str = "auto",
               run: bool = False, run_cmd: str = "") -> Task:
        """推送文件/目录到板子 (adb push), 可选推送后执行。

        - source:  本机 / SDK 内路径 (相对 SDK 根目录或绝对路径)
        - target:  设备端目标路径 (必须以 / 开头)
        - kind:    auto / file / directory
        - run:     推送后执行目标程序 (文件时先 chmod +x)
        - run_cmd: 推送后在设备端执行的命令 (优先于 run)

        执行位置跟随「设备连接位置」: 本机设备在本机 adb push;
        远端设备在编译机上直接 push (源在编译机, 无需拉回本机)。
        """
        if not source:
            raise ValueError(L("请指定要推送的源文件/目录"))
        if not target or not target.startswith("/"):
            raise ValueError(L("设备目标路径必须是以 / 开头的绝对路径"))

        # 1) 解析源路径 (相对 SDK 根目录 -> 绝对)
        src = source if self.conn.isabs(source) else self._abs(source)
        if not self._exists(src):
            raise ValueError(L("源不存在: {src}", src=src))

        # 2) 判断推送内容类型
        is_dir = self._is_dir(src) if kind == "auto" else (kind == "directory")

        adb = self._adb_cmd()
        cmd = f"{adb} push {shlex.quote(src)} {shlex.quote(target)}"
        if run_cmd:
            cmd += f" && {adb} shell {shlex.quote(run_cmd)}"
        elif run and not is_dir:
            cmd += (f" && {adb} shell chmod +x {shlex.quote(target)}"
                    f" && {adb} shell {shlex.quote(target)}")

        # 3) 远端设备模式: adb 与源都在编译机, 直接远端执行
        if self.conn.device_is_remote:
            return self.executor.exec_device(cmd)

        # 4) ssh 模式 + 本机设备: 源在编译机上, 先拉取到本机临时目录
        if self.conn.mode == "ssh":
            import tempfile
            base = os.path.join(tempfile.gettempdir(), "qpi_sdk_deploy")
            local = os.path.join(base, os.path.basename(src.rstrip("/")))
            if is_dir:
                self._pull_tree(src, local)
            else:
                self.executor.pull(src, local)
            src = local
            # 重新组装 (本机 adb push 本机文件)
            cmd = f"{adb} push {shlex.quote(src)} {shlex.quote(target)}"
            if run_cmd:
                cmd += f" && {adb} shell {shlex.quote(run_cmd)}"
            elif run and not is_dir:
                cmd += (f" && {adb} shell chmod +x {shlex.quote(target)}"
                        f" && {adb} shell {shlex.quote(target)}")

        # 5) adb 为 Windows 程序时 (Windows 宿主 / WSL interop), 源路径需 Windows 可读
        src = self._adb_local_arg(self.conn.win_path(src))
        cmd = f"{adb} push {shlex.quote(src)} {shlex.quote(target)}"
        if run_cmd:
            cmd += f" && {adb} shell {shlex.quote(run_cmd)}"
        elif run and not is_dir:
            cmd += (f" && {adb} shell chmod +x {shlex.quote(target)}"
                    f" && {adb} shell {shlex.quote(target)}")
        return self.executor.exec_local(cmd)

    def _is_dir(self, path: str) -> bool:
        """判断路径是否为目录 (local/WSL 直接判断; ssh 模式走 SFTP stat)。"""
        if self.conn.mode == "ssh":
            try:
                ssh = self.executor.ensure_ssh()
                sftp = ssh.ensure().open_sftp()
                try:
                    return bool(sftp.stat(path).st_mode & 0o040000)
                finally:
                    sftp.close()
            except Exception:
                return False
        try:
            return os.path.isdir(self.conn.win_path(path))
        except OSError:
            return False

    def _pull_tree(self, remote: str, local: str) -> None:
        """递归拉取远端目录到本机 (ssh 模式, 供目录推送前暂存)。"""
        ssh = self.executor.ensure_ssh()
        sftp = ssh.ensure().open_sftp()
        try:
            def rec(r: str, l: str) -> None:
                os.makedirs(l, exist_ok=True)
                for attr in sftp.listdir_attr(r):
                    rp = posixpath.join(r, attr.filename)
                    lp = os.path.join(l, attr.filename)
                    if attr.st_mode & 0o040000:
                        rec(rp, lp)
                    else:
                        sftp.get(rp, lp)
            rec(remote, local)
        finally:
            sftp.close()

    def dmesg(self, filter_str: str = "", follow: bool = False) -> Task:
        cmd = "dmesg" + (" -w" if follow else "")
        if filter_str:
            cmd += f" | grep -iE {shlex.quote(filter_str)}"
        return self.adb(f"shell {cmd}")

    def journalctl(self, filter_str: str = "", follow: bool = False) -> Task:
        cmd = "journalctl -b --no-pager" if not follow else "journalctl -b -f"
        if filter_str:
            cmd += f" | grep -iE {shlex.quote(filter_str)}"
        return self.adb(f"shell {cmd}")

    def reboot(self, edl: bool = False) -> Task:
        return self.adb("shell reboot edl" if edl else "shell reboot")

    def reboot_loader(self) -> Task:
        """进入下载模式 (按 SDK 平台选择命令)。

        - Qualcomm (H1): 用 VERSION.txt 的 ENTER_EDL, 如 `adb shell reboot edl`
        - Rockchip (M2): `adb reboot loader`
        """
        enter = str(self.platform().get("enter_loader") or "").strip()
        if enter:
            # VERSION.txt 里是完整命令 (含 adb 前缀), 去掉后交给 self.adb 统一执行
            return self.adb(re.sub(r"^adb\s+", "", enter))
        return self.adb("reboot loader")

    def device_info(self) -> dict:
        """采集设备详细信息 (只读诊断) 并返回结构化 JSON。

        通过单次 adb shell 脚本分段采集 (每段以 ==NAME== 标记), 在本机 Python 侧
        解析成分组字典。只返回实际能读取到的字段, 空字段直接省略, 供前端面板
        「有则展示、无则隐藏」。adb 始终在本机执行。
        """
        devs = self.adb_devices_parsed()
        dev = next((d for d in devs if d["state"] == "device"), None)
        if dev is None:
            if not devs:
                return {"ok": False, "error": L("未检测到 adb 设备 (请确认设备已连接并授权)")}
            return {"ok": False,
                    "error": L("adb 设备未就绪: {serial} [{state}]", serial=devs[0]['serial'], state=devs[0]['state'])}
        serial = dev["serial"]
        # 注意: 脚本内只用双引号 (外层经 shlex.quote 单引号包裹), 避免引号嵌套破坏。
        script = "\n".join([
            "echo ==MODEL==",
            "cat /proc/device-tree/model 2>/dev/null; echo",
            "echo ==SERIAL==",
            "grep -m1 \"^Serial\" /proc/cpuinfo 2>/dev/null | cut -d: -f2-",
            "echo ==COMPAT==",
            "cat /proc/device-tree/compatible 2>/dev/null | tr \"\\000\" \"\\012\" | head -8",
            "echo ==KERNEL==",
            "uname -r 2>/dev/null",
            "echo ==MACH==",
            "uname -m 2>/dev/null",
            "echo ==OS==",
            "grep PRETTY_NAME /etc/os-release 2>/dev/null | cut -d= -f2- | tr -d \"\\\"\"",
            "echo ==CPU==",
            "grep -m1 \"model name\" /proc/cpuinfo 2>/dev/null | cut -d: -f2-",
            # aarch64/arm 的 /proc/cpuinfo 没有 model name: 型号是独立一行
            # (如 "Rockchip Inc RK3576"), 取第一条不含冒号的非空行兜底。
            # x86 每行都带冒号, 这条兜底不会命中 (也不会覆盖上面的结果)。
            "grep -v \"^[[:space:]]*$\" /proc/cpuinfo 2>/dev/null | grep -m1 -v \":\"",
            "echo ==CORES==",
            "grep -c \"^processor\" /proc/cpuinfo 2>/dev/null",
            "echo ==LOAD==",
            "cat /proc/loadavg 2>/dev/null",
            "echo ==MEM==",
            "free -m 2>/dev/null",
            "echo ==ROOTDISK==",
            # 定位根文件系统所在的整盘: 多盘 (eMMC + SD/USB) 时用它决定"总容量"
            # 该报谁。root=PARTUUID=xxx 在 /dev/disk/by-partuuid/ 里按前缀匹配
            # (Rockchip 只写前半段), root=/dev/xxx 直接解析; 其它写法(UUID=/NFS)
            # 输出为空, 由主机侧退回"容量最大的盘"。
            "r=$(sed -n \"s/.*root=\\([^ ]*\\).*/\\1/p\" /proc/cmdline); "
            "case \"$r\" in "
            "/dev/*) readlink -f \"$r\" 2>/dev/null ;; "
            "PARTUUID=*) readlink -f /dev/disk/by-partuuid/${r#PARTUUID=}* 2>/dev/null ;; "
            "esac",
            "echo ==PART==",
            "cat /proc/partitions 2>/dev/null",
            "echo ==DF==",
            "df -h 2>/dev/null",
            "echo ==THERM==",
            "for z in /sys/class/thermal/thermal_zone*; do [ -f \"$z/type\" ] || continue; "
            "printf \"%s\\t%s\\t%s\\n\" \"$(basename \"$z\")\" \"$(cat \"$z/type\" 2>/dev/null)\" "
            "\"$(cat \"$z/temp\" 2>/dev/null)\"; done",
            "echo ==HOST==",
            "hostname 2>/dev/null",
            "echo ==NETADDR==",
            "ip -o addr show 2>/dev/null",
            "echo ==NETLINK==",
            "ip -o link show 2>/dev/null",
            "echo ==PROCS==",
            # 进程数只数进程 (线程不算)。procps 的 ps -e 列全部进程且带表头;
            # BusyBox 的 ps 没有 -e, 退回不带参数的 ps (同样是列全部进程)。
            # 主机侧只认首字段是 PID 的行, 表头/报错行自动跳过。
            "ps -e 2>/dev/null || ps 2>/dev/null",
            "echo ==UPTIME==",
            # 只要开机秒数 (第一列)。uptime 命令的整行还带当前时间/登录用户数/
            # 负载, 那些要么已有单独字段, 要么没意义, 不再往界面上丢整行。
            "cat /proc/uptime 2>/dev/null",
            "echo ==CMDLINE==",
            "cat /proc/cmdline 2>/dev/null",
            "echo ==END==",
        ])
        try:
            code, out, err = self.executor.exec_sync_device(
                f"{self._adb_cmd()} -s {shlex.quote(serial)} shell "
                f"{shlex.quote(script)}", timeout=40)
        except Exception as e:
            return {"ok": False, "error": L("采集设备信息失败: {e}", e=repr(e))}
        text = out or err or ""
        # adb shell 会分配 pty, iproute2 的 ip 命令因此带 ANSI 颜色码输出
        # (如 "2: \x1b[36mend0: \x1b[0m... link/ether \x1b[33m26:d3:...\x1b[0m"),
        # 会把网卡名/MAC/IP 包起来导致下面的正则匹配不到 (网卡列表一直是空的)。
        # 解析前统一清掉转义序列。
        text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)

        # ---- 按 ==NAME== 标记切分段落 ----
        sections: Dict[str, List[str]] = {}
        cur: Optional[str] = None
        for line in text.splitlines():
            # /proc/device-tree/* (model/compatible) 是带结尾 NUL 的定长字符串,
            # strip() 去不掉它, 会在字段尾部留下 \u0000, 这里一并清掉。
            line = line.rstrip("\r").replace("\x00", "")
            if line.startswith("==") and line.endswith("==") and len(line) > 4:
                cur = line[2:-2].strip()
                sections.setdefault(cur, [])
                continue
            if cur:
                sections.setdefault(cur, []).append(line)

        def first(name: str) -> str:
            for l in sections.get(name, []):
                if l.strip():
                    return l.strip()
            return ""

        info: dict = {"ok": True, "device": {"serial": serial, "state": dev["state"]}}

        # ---- 系统 ----
        system: dict = {}
        model = first("MODEL")
        if model:
            system["model"] = model
        sn = first("SERIAL")
        if sn:
            system["serial"] = sn
        compat = [l.strip() for l in sections.get("COMPAT", []) if l.strip()]
        if compat:
            system["compatible"] = compat
        kernel = first("KERNEL")
        if kernel:
            system["kernel"] = kernel
        mach = first("MACH")
        if mach:
            system["machine"] = mach
        os_name = first("OS")
        if os_name:
            system["os"] = os_name
        cmdline = first("CMDLINE")
        if cmdline:
            m = re.search(r"androidboot\.fwver=([^\s]+)", cmdline)
            if m:
                fw = {}
                for part in m.group(1).split(","):
                    k, _, v = part.partition("-")
                    if k and v:
                        fw[k] = v
                if fw:
                    system["fw"] = fw
            m = re.search(r"\broot=([^\s]+)", cmdline)
            if m:
                system["rootDev"] = m.group(1)
        if system:
            info["system"] = system

        # ---- 硬件 ----
        hw: dict = {}
        cpu = first("CPU")
        if cpu:
            hw["cpuModel"] = cpu
        cores = first("CORES")
        if cores and cores.isdigit():
            hw["cpuCores"] = int(cores)
        load = first("LOAD")
        if load:
            parts = load.split()
            try:
                hw["load"] = [round(float(parts[0]), 2), round(float(parts[1]), 2),
                              round(float(parts[2]), 2)]
            except (ValueError, IndexError):
                pass
        for l in sections.get("MEM", []):
            if l.startswith("Mem:"):
                cols = l.split()
                if len(cols) >= 7:
                    try:
                        hw["memTotalMB"] = int(cols[1])
                        hw["memUsedMB"] = int(cols[2])
                        hw["memAvailableMB"] = int(cols[6])
                    except ValueError:
                        pass
                break
        if hw:
            info["hardware"] = hw

        # ---- 存储 ----
        # /proc/partitions 里除了整盘, 还有 mmcblk0boot0/boot1/rpmb、ram/loop/zram
        # 这类独立"小盘"。以前用「名字里没有 p 就当整盘」判断, eMMC 的两个 boot 区
        # (各 4MiB) 会把真正的 mmcblk0 覆盖掉, 于是"总容量"显示 4MB。
        # 现在按名字归组: 形如 <盘名>p?<数字> 的算分区, 其余算盘; 整盘里优先取根
        # 文件系统所在的那个 (ROOTDISK), 拿不到就取容量最大的 —— mmcblk / sd /
        # nvme / ufs 各种命名都适用, 与具体板子无关。
        rows: List[tuple] = []
        for l in sections.get("PART", []):
            cols = l.split()
            if len(cols) != 4:
                continue
            name = cols[3]
            if name.startswith(("ram", "loop", "zram")):
                continue
            try:
                blocks = int(cols[2])
            except ValueError:
                continue
            if blocks:
                rows.append((name, blocks))
        names = {n for n, _ in rows}
        # 归属整盘: 取"最短"的匹配前缀。集合遍历顺序随机, 而 mmcblk0p10 既能被
        # mmcblk0 匹配也能被 mmcblk0p1 匹配 (余下 "0") —— 拿分区名当整盘, 该分区
        # 就会被排到别的组里从结果消失 (真机上 p10/p11 时有时无就是这个原因)。
        # 分区不会嵌套, 所以最短的合法前缀必然是整盘。
        part_owner: Dict[str, str] = {}
        for name in names:
            cands = [d for d in names
                     if re.fullmatch(re.escape(d) + r"p?\d+", name)]
            if cands:
                part_owner[name] = min(cands, key=len)
        disks: Dict[str, int] = {}       # 整盘名 -> 块数
        part_rows: List[tuple] = []      # (所属整盘, 分区名, 块数)
        for name, blocks in rows:
            owner = part_owner.get(name, "")
            if owner:
                part_rows.append((owner, name, blocks))
            else:
                disks[name] = blocks
        # ROOTDISK 形如 /dev/mmcblk0p9 -> 去掉路径和分区后缀得到整盘名
        root_disk = ""
        m = re.match(r"^(.*?)(p?\d+)$", (first("ROOTDISK") or "").rsplit("/", 1)[-1])
        if m and m.group(1) in disks:
            root_disk = m.group(1)
        if not root_disk and disks:
            root_disk = max(disks, key=lambda d: disks[d])
        storage: dict = {}
        if root_disk:
            storage["emmcMB"] = round(disks[root_disk] / 1024.0)
            part_list = [{"name": n, "sizeMB": round(b / 1024.0, 1)}
                         for owner, n, b in part_rows if owner == root_disk]
            if part_list:
                storage["partitions"] = part_list
        mounts: List[dict] = []
        for l in sections.get("DF", []):
            cols = l.split()
            if len(cols) < 6 or not cols[0].startswith("/dev/"):
                continue
            pct = cols[4].rstrip("%")
            mounts.append({"fs": cols[0], "size": cols[1], "used": cols[2],
                           "avail": cols[3],
                           "usePct": int(pct) if pct.isdigit() else 0,
                           "mount": cols[5]})
        if mounts:
            storage["mounts"] = mounts
        if storage:
            info["storage"] = storage

        # ---- 温度 / 电池监控 ----
        # /sys/class/thermal 下并非全是温度: Qualcomm 把电池电压 (vbat)、电池电流
        # (ibat-lvl*) 和限流档位 (bcl-lvl*) 也注册成了 thermal zone, 它们的 temp 不是
        # 毫摄氏度, 一律按 °C 显示会得到 "vbat: 3.9°C" 这类假数据。
        # 另外 quiet-thermal 这种通道板上没有有效热敏源, 读数恒定停在量程底端
        # (约 -41°C), 标记为 noData 而不是当成真实低温。
        thermal: List[dict] = []
        power: List[dict] = []
        for l in sections.get("THERM", []):
            cols = l.split("\t")
            if len(cols) < 3:
                continue
            name = cols[1] or cols[0]
            try:
                raw = int(cols[2])
            except ValueError:
                continue
            if name == "vbat":
                power.append({"name": name, "value": round(raw / 1000.0, 3),
                              "unit": "V", "kind": "voltage"})
            elif "ibat-lvl" in name:
                power.append({"name": name, "value": raw, "unit": "mA",
                              "kind": "current"})
            elif "bcl-lvl" in name:
                power.append({"name": name, "value": raw, "kind": "level"})
            elif raw <= -40000:
                thermal.append({"name": name, "noData": True})
            else:
                thermal.append({"name": name, "tempC": round(raw / 1000.0, 1)})
        if thermal:
            info["thermal"] = thermal
        if power:
            info["thermalPower"] = power

        # ---- 网络 ----
        ipv4_map: Dict[str, List[str]] = {}
        for l in sections.get("NETADDR", []):
            m = re.match(r"^\d+:\s+(\S+)\s+inet\s+([0-9.]+)/", l)
            if m and m.group(1) != "lo":
                ipv4_map.setdefault(m.group(1), []).append(m.group(2))
        mac_map: Dict[str, str] = {}
        for l in sections.get("NETLINK", []):
            m = re.match(r"^\d+:\s+([^:]+):.*link/ether\s+([0-9a-f:]+)", l)
            if m and m.group(1) != "lo":
                mac_map[m.group(1)] = m.group(2)
        ifaces = [{"name": n,
                   **({"ipv4": ipv4_map[n]} if n in ipv4_map else {}),
                   **({"mac": mac_map[n]} if n in mac_map else {})}
                  for n in sorted(set(list(ipv4_map) + list(mac_map)))]
        network: dict = {}
        host = first("HOST")
        if host:
            network["hostname"] = host
        if ifaces:
            network["interfaces"] = ifaces
        if network:
            info["network"] = network

        # ---- 运行时 ----
        runtime: dict = {}
        # /proc/uptime 第一列是开机秒数 (浮点), 前端按天/时/分组合成人话,
        # 不把 uptime 命令那整行 (时间/用户数/负载) 丢给界面。
        up = first("UPTIME").split()
        try:
            runtime["uptimeSec"] = int(float(up[0]))
        except (IndexError, ValueError):
            pass
        # 进程数: 只数进程, 线程不算。这里不用 /proc/loadavg —— 它第 4 段是
        # "运行中/总任务数"(含线程), 第 5 段是"最后分配的 PID"(单调递增, 每跑
        # 一条命令都会变大), 都不是进程数。统计口径见 ==PROCS== 段。
        n_proc = 0
        for l in sections.get("PROCS", []):
            cols = l.split()
            if cols and cols[0].isdigit():
                n_proc += 1
        if n_proc:
            runtime["procs"] = n_proc
        if runtime:
            info["runtime"] = runtime

        return info

    # ================= AT 指令 =================
    def _windows_serial_ports(self) -> List[dict]:
        """WSL 宿主下枚举 Windows 侧 COM 口 (WSL 内看不到 USB 串口)。

        失败时静默返回空列表: 枚举不到不影响 adb 通道与本机 pyserial 通道。
        """
        ports: List[dict] = []
        try:
            code, out, err = self.executor.exec_sync_local(
                _ps_command(_PS_ENUM_PORTS), timeout=30)
        except Exception:
            return ports
        for line in (out or "").splitlines():
            name, _, desc = line.partition("\t")
            name = name.strip().upper()
            if not _COM_RE.match(name):
                continue
            desc = desc.strip()
            ports.append({
                "device": name,
                "desc": L("Windows 串口 (WSL 转发)") + (": " + desc if desc else ""),
                "kind": "windows",
            })
        return ports

    def list_serial_ports(self) -> List[dict]:
        """枚举可用串口。

        返回条目: {device, desc, kind, adb_serial?}
        - kind "local"   : 本机 COM/tty (pyserial)
        - kind "remote"  : 远端编译机的 /dev/tty* (device_location=remote)
        - kind "windows" : WSL 宿主下的 Windows COM 口
        - kind "adb"     : adb 端侧设备的串口 (adb shell), 用于 AT 经 adb shell 直连
        adb 条目用于 AT 指令通过 adb shell 直连板子上的串口
        (如 /dev/ttyS1、/dev/ttyUSB2), 解决 AT 口在端侧设备上的场景。
        WSL 宿主下额外列出 Windows COM 口 (kind="windows"): 真实 USB 串口只存在于
        Windows 侧, WSL 内的 /dev/ttyS* 是内核虚拟口, 列出来只会误导。
        远端设备 (device_location=remote): 板子 USB 接编译机, 串口全部在编译机枚举。
        """
        if self.conn.device_is_remote:
            ports: List[dict] = []
            # 编译机上的物理串口 (板子 USB 转串口 / USB 调试串口)
            try:
                code, out, err = self.executor.exec_sync_device(
                    "ls /dev/ttyUSB* /dev/ttyACM* /dev/ttyHS* 2>/dev/null; "
                    "ls /dev/ttyS* 2>/dev/null", timeout=8)
                seen: set = set()
                for path in (out or "").split():
                    if path not in seen:
                        seen.add(path)
                        ports.append({"device": path, "desc": L("远程串口 (编译机)"),
                                      "kind": "remote"})
            except Exception:
                pass
            ports.extend(self._adb_serial_ports())
            return ports
        fwd = _wsl_serial_forward()
        ports: List[dict] = []
        if fwd:
            ports.extend(self._windows_serial_ports())
        try:
            import serial.tools.list_ports as lp
            for p in lp.comports():
                # WSL 下过滤掉打不开的端口 (虚拟 ttyS 属 root 且不接设备);
                # 能访问的口 (如 usbipd 直通的 /dev/ttyUSB0) 照常保留。
                if fwd and not os.access(p.device, os.R_OK | os.W_OK):
                    continue
                ports.append({"device": p.device, "desc": p.description or "",
                              "kind": "local"})
        except Exception:
            pass
        ports.extend(self._adb_serial_ports())
        return ports

    def _adb_serial_ports(self) -> List[dict]:
        """adb 端侧设备串口 (跟随设备连接位置在对应机器上枚举)。"""
        ports: List[dict] = []
        try:
            for d in self.adb_devices_parsed():
                if d["state"] != "device":
                    continue
                serial = d["serial"]
                code, out, err = self.executor.exec_sync_device(
                    f"{self._adb_cmd()} -s {shlex.quote(serial)} shell "
                    f"'ls /dev/ttyS* /dev/ttyUSB* /dev/ttyACM* /dev/ttyHS* 2>/dev/null'",
                    timeout=8)
                text = (out or "").strip()
                for path in text.split():
                    ports.append({"device": path,
                                  "desc": L("端侧设备 adb:{serial}", serial=serial),
                                  "kind": "adb", "adb_serial": serial})
        except Exception:
            pass
        return ports

    # pyserial 收发脚本 (本机 venv python 与远端 python3 共用)。
    # 参数顺序: port baud bytesize parity stopbits flow cmd...
    # write_timeout 防止设备不读/流控异常时 write() 无限阻塞;
    # 流控由 flow 决定 (none=全关, sw=XON/XOFF, hw=RTS/CTS),
    # 默认关闭 (ttyHS 类口默认可能开启)。
    _AT_SERIAL_SCRIPT = (
        "import serial,sys,time\n"
        "port,baud=sys.argv[1],int(sys.argv[2])\n"
        "bytesize,parity,stopbits,flow=(int(sys.argv[3]),sys.argv[4],"
        "float(sys.argv[5]),sys.argv[6])\n"
        "print(f'[AT] 打开串口 {port} @ {baud} {bytesize}{parity}{stopbits:g} "
        "流控={flow} ...',flush=True)\n"
        "try:\n"
        "    p=serial.Serial(port,baud,bytesize=bytesize,parity=parity,"
        "stopbits=stopbits,\n"
        "                    timeout=0.2,write_timeout=2,rtscts=(flow=='hw'),\n"
        "                    xonxoff=(flow=='sw'),dsrdtr=False)\n"
        "    print('[AT] 串口打开成功',flush=True)\n"
        "    for c in sys.argv[7:]:\n"
        "        print(f'[AT] >>> 发送: {c}',flush=True)\n"
        "        p.write((c+'\\r\\n').encode())\n"
        "        time.sleep(0.3)\n"
        "        buf=[]\n"
        "        for _ in range(15):\n"
        "            d=p.read(512)\n"
        "            if d: buf.append(d.decode(errors='replace'))\n"
        "        print(''.join(buf) if buf else '(无回复)',flush=True)\n"
        "    p.close()\n"
        "    print('[AT] 串口已关闭',flush=True)\n"
        "except Exception as e:\n"
        "    print(f'[AT] 串口操作失败: {e!r}',flush=True)\n"
        "    sys.exit(1)\n"
    )

    # 串口帧参数取值与各通道的映射 (pyserial / PowerShell / stty 三套写法)
    _AT_PARITY = ("N", "E", "O", "M", "S")            # 无/偶/奇/Mark/Space
    _AT_FLOW = ("none", "sw", "hw")                   # 无 / 软 (XON-XOFF) / 硬 (RTS-CTS)
    _AT_STOPBITS = (1, 1.5, 2)
    _PS_PARITY = {"N": "None", "E": "Even", "O": "Odd", "M": "Mark", "S": "Space"}
    _PS_STOPBITS = {1: "One", 1.5: "OnePointFive", 2: "Two"}
    _PS_HANDSHAKE = {"none": "None", "sw": "XOnXOff", "hw": "RequestToSend"}
    _STTY_PARITY = {"N": "-parenb", "E": "parenb -parodd", "O": "parenb parodd",
                    "M": "parenb parodd cmspar", "S": "parenb -parodd cmspar"}
    _STTY_FLOW = {"none": "-crtscts -ixon -ixoff", "sw": "-crtscts ixon ixoff",
                  "hw": "crtscts -ixon -ixoff"}

    def at(self, cmds: List[str], port: str = "/dev/ttyACM0",
           adb_serial: str = "", baud: int = 115200, bytesize: int = 8,
           parity: str = "N", stopbits: float = 1, flow: str = "none") -> Task:
        """通过串口发送 AT 指令。

        帧参数: 波特率 baud; 数据位 bytesize (5-8, 4 仅 adb 通道);
        parity ∈ N/E/O/M/S; stopbits ∈ 1/1.5/2; flow ∈ none/sw/hw。

        通道 (按「设备连接位置」):
        - adb_serial 非空: 通过 adb shell 向端侧设备 (板子) 上的串口发送,
          适用于 AT 口在板子上的场景 (如 /dev/ttyS1、/dev/ttyUSB2)。
        - 否则: 用 pyserial 打开串口。
          * 本机设备: 本机串口 (Windows COM口 / Linux tty), WSL 宿主自动改走
            PowerShell 打开 Windows COM 口 (见 _at_via_windows)。
          * 远端设备: 远端编译机的串口 (需远端装 python3 + pyserial)。
        """
        if parity not in self._AT_PARITY:
            raise ValueError(L("不支持的校验位: {parity} (可选 N/E/O/M/S)", parity=parity))
        if flow not in self._AT_FLOW:
            raise ValueError(L("不支持的流控: {flow} (可选 none/sw/hw)", flow=flow))
        if stopbits not in self._AT_STOPBITS:
            raise ValueError(L("不支持的停止位: {stopbits} (可选 1/1.5/2)", stopbits=stopbits))
        if adb_serial:
            return self._at_via_adb(cmds, port, adb_serial, baud,
                                    bytesize, parity, stopbits, flow)
        if bytesize == 4:
            # stty (adb 通道) 有 cs4, pyserial 只认 5-8, .NET DataBits 亦为 5-8
            raise ValueError(L("数据位 4 仅支持 adb 通道 (端侧设备串口); 本机/远端串口只支持 5-8"))
        if self.conn.device_is_remote:
            return self._at_via_remote_serial(cmds, port, baud,
                                              bytesize, parity, stopbits, flow)
        # WSL 宿主: 真实串口在 Windows 侧, 经 interop 用 PowerShell 打开 COM 口
        if _wsl_serial_forward() and _COM_RE.match(port or ""):
            return self._at_via_windows(cmds, port, baud,
                                        bytesize, parity, stopbits, flow)
        args = " ".join(shlex.quote(c) for c in cmds)
        venv_py = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            ".venv",
            "Scripts" if os.name == "nt" else "bin",
            "python.exe" if os.name == "nt" else "python",
        )
        py_exe = venv_py if os.path.isfile(venv_py) else sys.executable
        return self.executor.exec_local(
            f"{shlex.quote(py_exe)} -c {shlex.quote(self._AT_SERIAL_SCRIPT)} "
            f"{shlex.quote(port)} {baud} {bytesize} {parity} {stopbits:g} {flow} {args}")

    def _at_via_remote_serial(self, cmds: List[str], port: str,
                              baud: int = 115200, bytesize: int = 8,
                              parity: str = "N", stopbits: float = 1,
                              flow: str = "none") -> Task:
        """远端设备模式下经远端 python3 + pyserial 打开远端串口发 AT 指令。

        远端为 Linux 编译机: 要求已安装 python3 与 pyserial, 缺失时命令内给出提示。
        """
        args = " ".join(shlex.quote(c) for c in cmds)
        check = (L("python3 -c 'import serial' 2>/dev/null || "
                 "(echo '[AT] 远端缺少 pyserial, 请先在编译机执行: pip3 install pyserial' "
                 "&& exit 1)"))
        cmd = (f"{check}; python3 -c {shlex.quote(self._AT_SERIAL_SCRIPT)} "
               f"{shlex.quote(port)} {baud} {bytesize} {parity} {stopbits:g} {flow} {args}")
        return self.executor.exec_device(cmd)

    def _at_via_windows(self, cmds: List[str], port: str,
                        baud: int = 115200, bytesize: int = 8,
                        parity: str = "N", stopbits: float = 1,
                        flow: str = "none") -> Task:
        """WSL 宿主下经 Windows PowerShell 向 Windows COM 口发 AT 指令。

        WSL2 无 USB 直通, Linux 版 pyserial 在这里打不开真实串口, 所以把整个收发
        过程交给 Windows 进程 (System.IO.Ports)。仅 WSL 下被 at() 调用, 不影响
        Windows / 原生 Linux 的 pyserial 路径。
        """
        if not _COM_RE.match(port or ""):
            raise ValueError(L("非法的 Windows 串口名: {port}", port=port))
        script = (_PS_AT_TEMPLATE
                  .replace("__PORT__", port.upper())
                  .replace("__BAUD__", str(int(baud)))
                  .replace("__BYTESIZE__", str(int(bytesize)))
                  .replace("__PARITY__", self._PS_PARITY[parity])
                  .replace("__STOPBITS__", self._PS_STOPBITS[stopbits])
                  .replace("__FLOW__", self._PS_HANDSHAKE[flow])
                  .replace("__CMDS__", ",".join(_ps_quote(c) for c in cmds)))
        return self.executor.exec_local(_ps_command(script))

    def _at_via_adb(self, cmds: List[str], port: str, serial: str,
                    baud: int = 115200, bytesize: int = 8,
                    parity: str = "N", stopbits: float = 1,
                    flow: str = "none") -> Task:
        """通过 adb shell 向端侧设备的串口发送 AT 指令。

        设备侧命令全部带 timeout, 防止串口流控异常导致任务挂死;
        指令内容用 base64 传输, 避免引号/特殊字符转义问题。
        """
        import base64
        if not re.match(r"^/dev/[A-Za-z0-9_./-]+$", port):
            raise ValueError(L("非法的设备串口路径: {port}", port=port))
        adb = self._adb_cmd()
        q = chr(34)
        # stty 在 raw 之后再设帧参数 (后设者生效), clocal 避免写阻塞;
        # Mark/Space 需要 cmspar (Linux 专有, 设备侧 stty 不支持时会报错退出)
        opt = (f"cs{bytesize} {self._STTY_PARITY[parity]} "
               f"{'cstopb' if stopbits != 1 else '-cstopb'} {self._STTY_FLOW[flow]}")
        steps = []
        for c in cmds:
            b64 = base64.b64encode((c + chr(13) + chr(10)).encode("utf-8", "replace")).decode()
            inner = (
                L("if stty -F {port} {baud} raw -echo clocal {opt} 2>/dev/null; then echo {b64} | base64 -d > {port} 2>/dev/null; sleep 0.3; echo {b64} | base64 -d > {port} 2>/dev/null; timeout 2 cat {port} 2>/dev/null; else echo {q}无法配置串口 {port} (不存在或权限不足){q}; exit 1; fi", port=port, baud=baud, opt=opt, b64=b64, q=q)
            )
            # 设备侧整条脚本用 timeout 包住: stty/写入/串口流控异常时,
            # 不会让 adb shell 无限挂起 (仅 cat 有 timeout 不够, 见 #AT-hang)
            dev_cmd = f"timeout 15 sh -c {shlex.quote(inner)}"
            steps.append(f"echo {shlex.quote('--- ' + c + ' ---')}")
            steps.append(f"{adb} -s {shlex.quote(serial)} shell {shlex.quote(dev_cmd)}")
        return self.executor.exec_device(" && ".join(steps))

    # ================= 音频 =================
    def audio_collect_logs(self) -> Task:
        """一键抓音频日志: 混音器状态 + dmesg(音频相关), 保存到设备操作所在机器。

        H1/QCS6490 (Android 系) 有 tinymix; M2/RK3576 (Debian) 没有, 回退 amixer
        逐张声卡导出, 避免产物为空。
        """
        script = self._abs("tools/collect_audio_logs.sh")
        # ssh 模式 + 本机设备: 脚本在编译机 (其 adb 在编译机), 与接本机的板子不匹配,
        # 改走下方本机 adb 抓取; 其余场景脚本在设备操作所在的机器, 直接用。
        if self._exists(script) and not (self.conn.mode == "ssh"
                                         and not self.conn.device_is_remote):
            return self.executor.exec_device(
                f"cd {shlex.quote(self.conn.sdk_abs)} && {shlex.quote(script)}")
        adb = self._adb_cmd()
        dev_capture = shlex.quote(self._audio_state_dev_cmd())
        if self.conn.device_is_remote:
            # 远端设备模式: 日志保存在编译机 $HOME (跟随设备操作所在机器)
            local_dir = "$HOME/qpi_sdk_logs/audio"
            cmd = (L("mkdir -p {dir} && {adb} shell {dev_capture} && {adb} pull /data/local/tmp/tinymix.txt {pull_dir}/ && {adb} pull /data/local/tmp/audio_dmesg.txt {pull_dir}/ && echo '音频日志已保存到 {dir}/'", dir=shlex.quote(local_dir), adb=adb, dev_capture=dev_capture, pull_dir=shlex.quote(local_dir))
                   + self._audio_empty_warn(local_dir))
            return self.executor.exec_device(cmd)
        local_dir = os.path.join(os.path.expanduser("~"), "qpi_sdk_logs", "audio")
        os.makedirs(local_dir, exist_ok=True)
        # dir 给 bash 侧 (mkdir/校验) 用 WSL 路径; pull_dir 给 adb 侧 (Windows 程序时转 UNC)
        cmd = (L("mkdir -p {dir} && {adb} shell {dev_capture} && {adb} pull /data/local/tmp/tinymix.txt {pull_dir}/ && {adb} pull /data/local/tmp/audio_dmesg.txt {pull_dir}/ && echo '音频日志已保存到 {dir}/'", dir=shlex.quote(local_dir), adb=adb, dev_capture=dev_capture, pull_dir=shlex.quote(self._adb_local_arg(local_dir)))
               + self._audio_empty_warn(local_dir))
        return self.executor.exec_local(cmd)

    @staticmethod
    def _audio_state_dev_cmd() -> str:
        """设备端导出混音器状态 + 音频 dmesg。

        RK/Debian 板卡 (如 M2) 无 tinymix; 且 amixer 不带 -c 会因没有默认声卡而
        报 "Mixer attach default error", 所以按 /sys/class/sound/cardN 逐张导出。
        """
        return (
            'mkdir -p /data/local/tmp; '
            'if command -v tinymix >/dev/null 2>&1; then '
            'tinymix > /data/local/tmp/tinymix.txt 2>&1; '
            'else '
            ': > /data/local/tmp/tinymix.txt; '
            'for s in /sys/class/sound/card[0-9]*; do '
            '[ -e "$s" ] || continue; '
            'c=$(echo "$s" | sed "s/.*card//"); '
            'echo "===== card $c =====" >> /data/local/tmp/tinymix.txt; '
            'amixer -c "$c" contents >> /data/local/tmp/tinymix.txt 2>&1; '
            'done; '
            'fi; '
            'dmesg | grep -iE "audio|codec|i2s|sound" > /data/local/tmp/audio_dmesg.txt'
        )

    @staticmethod
    def _audio_empty_warn(local_dir: str) -> str:
        """抓取后校验产物: 无 tinymix/amixer 或 dmesg 受限时会生成空文件, 需明确告警。"""
        d = shlex.quote(local_dir)
        return (
            L("; [ -s {d}/tinymix.txt ] || echo '[QPi] 警告: tinymix.txt 为空 (设备既无 tinymix 也无 amixer, 或权限不足)'; [ -s {d}/audio_dmesg.txt ] || echo '[QPi] 警告: audio_dmesg.txt 为空 (dmesg 可能无权限或无匹配日志)'", d=d)
        )

    def tinymix(self, args: str) -> Task:
        """音频控件读写: 优先 tinymix (Android), 无则按等价命令回退 amixer。

        M2/RK3576 等 Debian 板卡只有 amixer, 常见调用做等价映射:
        controls -> amixer -c 0 controls; get X -> sget X; set X V -> sset X V。
        """
        raw = (args or "").strip()
        verb, _, rest = raw.partition(" ")
        if raw in ("", "controls"):
            amix = "amixer -c 0 controls"
        elif verb in ("get", "set"):
            amix = f"amixer -c 0 {'sget' if verb == 'get' else 'sset'} {rest}"
        else:
            amix = f"amixer -c 0 {raw}"
        dev = (L("if command -v tinymix >/dev/null 2>&1; then tinymix {raw}; elif command -v amixer >/dev/null 2>&1; then {amix}; else echo '[QPi] 设备无 tinymix/amixer' >&2; exit 1; fi", raw=raw, amix=amix))
        return self.adb(f"shell {shlex.quote(dev)}")

    # ================= 媒体播放 (音频/视频) =================
    @staticmethod
    def _media_play_dev_script() -> str:
        """设备端播放助手脚本: 在图形桌面会话用户下播放音频/视频。

        为什么必须切到会话用户 (与截图/录屏同一套做法):
        - adb shell 默认是 root, 其 XDG_RUNTIME_DIR 指向 /var/run, 连不上
          /run/user/<uid>/ 下的 PipeWire(音频) 与 Wayland(视频) socket, 播放器
          初始化会直接失败;
        - 即使绕开框架直接开 hw: 声卡, 也会被 PipeWire 独占 -> Device or resource busy。
        会话用户与运行时目录从 /run/user/*/wayland-0 的属主推导。

        播放器按能力从高到低回退, 任一成功即结束:
          音频 pw-play -> paplay -> aplay
          视频 gst-play-1.0 (Rockchip MPP 硬解) -> mpv -> ffplay
        """
        return r'''#!/bin/sh
mode="$1"; f="$2"
if [ -z "$f" ]; then echo "[QPi] 缺少文件参数" >&2; exit 2; fi

# 会话用户: /run/user/*/wayland-0 的属主即已登录桌面会话的用户
U=$(stat -c %U /run/user/*/wayland-0 2>/dev/null | head -n1)
[ -n "$U" ] || U=$(stat -c %U /run/user/*/pipewire-0 2>/dev/null | head -n1)
[ -n "$U" ] || U=$(getent passwd 1000 2>/dev/null | cut -d: -f1)
N=$(id -u "$U" 2>/dev/null)
[ -n "$N" ] || { echo "[QPi] 未找到图形桌面会话 (需要已登录的桌面会话)" >&2; exit 1; }
RD="/run/user/$N"
WD=$(ls "$RD" 2>/dev/null | grep -m1 '^wayland-' || true)
[ -n "$WD" ] || WD=wayland-0
echo "[QPi] 会话用户=$U XDG_RUNTIME_DIR=$RD WAYLAND_DISPLAY=$WD"

# 当前已是会话用户时只需导出环境; 否则用 runuser 切换
if [ "$(id -u)" = "0" ] && [ "$N" != "0" ]; then
  SESS="runuser -u $U -- env XDG_RUNTIME_DIR=$RD WAYLAND_DISPLAY=$WD DISPLAY=${DISPLAY:-:0} DBUS_SESSION_BUS_ADDRESS=unix:path=$RD/bus"
else
  XDG_RUNTIME_DIR=$RD; export XDG_RUNTIME_DIR
  WAYLAND_DISPLAY=$WD; export WAYLAND_DISPLAY
  SESS=""
fi

if [ ! -f "$f" ]; then
  echo "[QPi] 文件不存在: $f" >&2
  if [ "$mode" = "audio" ] && command -v ffmpeg >/dev/null 2>&1; then
    echo "[QPi] 用设备自带 ffmpeg 现场生成 3 秒 440Hz 测试音频 ..." >&2
    ffmpeg -hide_banner -loglevel error -f lavfi -i "sine=frequency=440:duration=3" -ar 48000 -ac 2 -y "$f" >/dev/null 2>&1
    [ -s "$f" ] || { echo "[QPi] 生成测试音频失败" >&2; exit 1; }
  else
    echo "[QPi] 请先生成测试文件, 例如:" >&2
    echo "  ffmpeg -f lavfi -i testsrc=size=1280x720:rate=30 -f lavfi -i sine=frequency=440 -t 10 -c:v libx264 -pix_fmt yuv420p -c:a aac $f" >&2
    exit 1
  fi
fi

if [ "$mode" = "video" ]; then
  if command -v gst-play-1.0 >/dev/null 2>&1; then
    echo "[QPi] 播放器: gst-play-1.0 (硬件解码)"
    exec $SESS gst-play-1.0 --no-interactive "$f" < /dev/null
  fi
  if command -v mpv >/dev/null 2>&1; then
    echo "[QPi] 播放器: mpv"
    exec $SESS mpv --no-config --fs "$f" < /dev/null
  fi
  if command -v ffplay >/dev/null 2>&1; then
    echo "[QPi] 播放器: ffplay"
    exec $SESS ffplay -autoexit -loglevel warning "$f" < /dev/null
  fi
  echo "[QPi] 设备无可用视频播放器 (gst-play-1.0 / mpv / ffplay)" >&2
  exit 1
fi

echo "[QPi] 文件: $f"
# timeout 仅作挂死保护 (播放器卡住时才会触发; 正常由播放结束或停止按钮收尾)
for t in pw-play paplay aplay; do
  command -v "$t" >/dev/null 2>&1 || continue
  echo "[QPi] 播放器: $t"
  if [ -n "$SESS" ]; then timeout 3600 $SESS "$t" "$f"; else timeout 3600 "$t" "$f"; fi
  rc=$?
  [ "$rc" = "0" ] && { echo "[QPi] 播放完成"; exit 0; }
  echo "[QPi] $t 失败 (exit $rc), 尝试下一个播放器 ..." >&2
done
echo "[QPi] 所有播放器均失败 (pw-play / paplay / aplay)" >&2
exit 1
'''

    def _media_play(self, mode: str, file: str, kill_pattern: str) -> Task:
        """音频/视频播放的公共实现 (设备端助手见 _media_play_dev_script)。

        助手脚本先经 base64 写到设备再执行: 播放器回退链里有大量引号/竖线, 直接
        内联进 adb shell 容易被转义破坏, 也可能触发 "shell command too long"。
        设备侧播放进程不会随本机 adb 客户端退出而结束 (会一直占着声卡与屏幕),
        因此注册 kill_cmd, 停止任务时在设备侧 pkill 收尾。
        """
        b64 = base64.b64encode(self._media_play_dev_script().encode()).decode()
        dev_script = "/tmp/qpi_media_play.sh"
        adb = self._adb_cmd()
        run = (f"sh {shlex.quote(dev_script)} {shlex.quote(mode)} "
               f"{shlex.quote(file)}")
        task = self.executor.exec_device(
            f"{self._helper_write_cmd(adb, b64, dev_script)}"
            f"{adb} shell {shlex.quote(run)}")
        # 用 [x] 形式写模式, 避免 pkill 匹配到自己的命令行
        task.kill_cmd = (
            f"{adb} shell "
            f"{shlex.quote('pkill -9 -f ' + kill_pattern + ' 2>/dev/null; true')}")
        return task

    def aplay(self, file: str = "/tmp/test.wav") -> Task:
        """音频播放: 切到设备桌面会话用户走 PipeWire/PulseAudio。

        依次尝试 pw-play -> paplay -> aplay (板子无某播放器时自动跳过);
        文件不存在时用设备自带 ffmpeg 现场生成 3 秒 440Hz 测试音频。
        """
        return self._media_play("audio", file,
                                "[p]w-play|[p]aplay|[a]play|[q]pi_media_play")

    def play_video(self, file: str = "/tmp/test.mp4") -> Task:
        """视频播放: 切到设备桌面会话用户, 输出到板子屏幕。

        优先 gst-play-1.0 (走 Rockchip MPP 硬解), 回退 mpv / ffplay;
        文件不存在时提示生成测试视频的 ffmpeg 命令 (视频编码较慢, 不自动生成)。
        """
        return self._media_play("video", file,
                                "[g]st-play|[m]pv|[f]fplay|[q]pi_media_play")

    # ================= 工具 =================
    @staticmethod
    def _helper_write_cmd(adb: str, b64: str,
                          dev_path: str = "/tmp/qpi_screen_record.py") -> str:
        """生成「把设备端助手脚本写到设备」的命令序列。

        助手脚本用 base64 分片经 printf 管道解码写入:
        - 不能整段内联进一条 adb shell: 会触发 adb "shell command too long";
        - 不能用主机侧 stdin 管道 (echo | adb shell ...): 新版 adb shell 会分配
          pty, 远端 base64 等不到 EOF 会一直挂住。
        分片长度取 2000 (4 的倍数, 每片都能独立 base64 解码)。
        """
        step = 2000
        parts = []
        for i in range(0, len(b64), step):
            chunk = b64[i:i + step]
            redir = ">" if i == 0 else ">>"
            parts.append(
                f"{adb} shell \"printf '%s' '{chunk}' | base64 -d "
                f"{redir} {dev_path}\"")
        return "; ".join(parts) + "; "

    def screenshot(self, filename: str = "") -> Task:
        """截图: 保存到设备操作所在的机器 (~/qpi_sdk_logs/screenshots)。

        Android 有 screencap 时直接用它; Debian/GNOME (Wayland) SDK 上没有该命令,
        改用 GNOME Screencast 录一小段, 再用设备上的 gst 抽首帧成 PNG。
        (GNOME 41 起 Screenshot D-Bus 只允许 shell 内部调用 -> AccessDenied;
        XDG portal 的 Screenshot 实测返回 code=2, 两条路都不可用。)
        """
        name = filename or "screenshot_$(date +%Y%m%d_%H%M%S).png"
        # 复用录屏的设备端助手 (GNOME Shell Screencast)
        b64 = base64.b64encode(self._SCREEN_RECORD_PY.encode()).decode()
        dev_script = f"""\
mkdir -p /data/local/tmp
rm -f /data/local/tmp/qpi_shot.png
if command -v screencap >/dev/null 2>&1; then
  screencap -p /data/local/tmp/qpi_shot.png
else
  command -v python3 >/dev/null 2>&1 || {{ echo "[QPi] 设备无 python3, 无法截图" >&2; exit 1; }}
  command -v gst-launch-1.0 >/dev/null 2>&1 || {{ echo "[QPi] 设备无 gst-launch-1.0, 无法截图" >&2; exit 1; }}
  U=$(stat -c %U /run/user/*/wayland-0 2>/dev/null | head -n1)
  [ -n "$U" ] || {{ echo "[QPi] 未找到图形桌面会话 (需要 GNOME/Wayland 桌面)" >&2; exit 1; }}
  N=$(id -u "$U")
  # M2/RK3576 的 D-Bus 激活环境缺 WAYLAND_DISPLAY/DISPLAY, 会让 Screencast
  # 服务 (gjs 启动时 Gtk.init) 直接退出; 从会话环境读真实值交给助手推送。
  SENV=$(runuser -u "$U" -- env XDG_RUNTIME_DIR=/run/user/$N systemctl --user show-environment 2>/dev/null)
  WD=$(printf '%s\\n' "$SENV" | sed -n 's/^WAYLAND_DISPLAY=//p' | head -n1)
  XD=$(printf '%s\\n' "$SENV" | sed -n 's/^DISPLAY=//p' | head -n1)
  [ -n "$WD" ] || WD=wayland-0
  [ -n "$XD" ] || XD=:0
  echo "[QPi] 截图 (录 2s 抽首帧, 会话用户: $U) ..."
  rm -rf /tmp/qpi_shot_rec
  rec=$(runuser -u "$U" -- env XDG_RUNTIME_DIR=/run/user/$N \\
        DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$N/bus \\
        WAYLAND_DISPLAY=$WD DISPLAY=$XD \\
        python3 /tmp/qpi_screen_record.py /tmp/qpi_shot_rec/qpi_shot 2)
  # 助手失败时 $rec 为空 (真正的报错已由助手打到 stderr), 这里直接收尾, 避免
  # 拿空路径去跑 gst 再抛一串 "No such file" 干扰判断。
  [ -n "$rec" ] || {{ echo "[QPi] 截屏失败: 会话内未能启动 GNOME Screencast (会话用户: $U, 请确认该用户已登录桌面而非停在登录界面)" >&2; exit 1; }}
  # H1/QCS6490 上 decodebin 会优先选硬解 v4l2h264dec, 但该平台协商失败
  # ("V4L2 format Q08C not supported"), 会写出 0 字节 PNG。先把 v4l2 解码器
  # 降权让 decodebin 用软件解码 (libav); 该板若没有软件解码器再退回默认选择。
  if ! GST_PLUGIN_FEATURE_RANK=v4l2h264dec:0,v4l2vp8dec:0 \\
       gst-launch-1.0 -q -e filesrc location="$rec" ! decodebin ! videoconvert \\
       ! pngenc snapshot=true ! filesink location=/data/local/tmp/qpi_shot.png 2>&1; then
    echo "[QPi] 软解抽帧失败, 改用默认解码器重试" >&2
    gst-launch-1.0 -q -e filesrc location="$rec" ! decodebin ! videoconvert \\
        ! pngenc snapshot=true ! filesink location=/data/local/tmp/qpi_shot.png >&2
  fi
  rm -rf /tmp/qpi_shot_rec
fi
if [ -e /data/local/tmp/qpi_shot.png ]; then ls -l /data/local/tmp/qpi_shot.png; fi
"""
        adb = self._adb_cmd()  # 先解析, 以确定后续本机路径是否需要转成 Windows 形式
        if self.conn.device_is_remote:
            # 远端设备模式: 截图保存在编译机 $HOME (跟随设备操作所在机器)
            out_dir = "$HOME/qpi_sdk_logs/screenshots"
            pull_dir = out_dir
        else:
            shot_dir = os.path.join(os.path.expanduser("~"),
                                    "qpi_sdk_logs", "screenshots")
            os.makedirs(shot_dir, exist_ok=True)
            out_dir = shlex.quote(shot_dir)          # bash 侧 (mkdir/校验) 用 WSL 路径
            pull_dir = shlex.quote(self._adb_local_arg(shot_dir))  # adb 侧用可读写路径
        # adb shell 不一定回传设备侧退出码 (部分平台恒为 0), 故主机侧统一
        # 用「拉回的文件是否非空」判定成败, 避免 0 字节 PNG 被当成截图成功。
        cmd = (L("mkdir -p {out_dir} && {helper}{adb} shell {dev_script}; dest={out_dir}/{name}; destwin={pull_dir}/{name}; {adb} pull /data/local/tmp/qpi_shot.png \"$destwin\"; if [ -s \"$dest\" ]; then echo \"截图已保存: $dest\"; else rm -f \"$dest\"; echo '[QPi] 截图失败: 未生成有效图片 (需要已登录的 GNOME/Wayland 桌面会话; 设备侧原因见上方输出)' >&2; exit 1; fi", out_dir=out_dir, helper=self._helper_write_cmd(adb, b64), adb=adb, pull_dir=pull_dir, dev_script=shlex.quote(dev_script), name=name))
        return self.executor.exec_device(cmd) if self.conn.device_is_remote \
            else self.executor.exec_local(cmd)

    # 设备端录屏助手: Debian/GNOME 系 SDK 没有 screenrecord 命令, 改调用 GNOME Shell
    # 自带的 Screencast D-Bus 接口录成 h264/mp4。必须由常驻进程调用 —— GNOME 在调用方
    # D-Bus 连接断开时会立刻中止录制, 所以 gdbus/busctl 这类一次性命令不可用。
    _SCREEN_RECORD_PY = """\
#!/usr/bin/env python3
\"\"\"设备端录屏助手: 通过 GNOME Shell Screencast (Wayland) 录制屏幕。
用法: python3 qpi_screen_record.py <输出路径前缀> <秒数>
GNOME 会在前缀后自动补 .mp4, 实际路径从 stdout 输出。
\"\"\"
import os
import sys
import time

import gi
gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib

DEST = "org.gnome.Shell.Screencast"
PATH = "/org/gnome/Shell/Screencast"
IFACE = "org.gnome.Shell.Screencast"

out = sys.argv[1]
secs = float(sys.argv[2]) if len(sys.argv) > 2 else 10.0

os.makedirs(os.path.dirname(out), exist_ok=True)
bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)


def _push_activation_env():
    # org.gnome.Shell.Screencast 是 D-Bus 激活的 gjs 服务, 启动时会 Gtk.init();
    # M2/RK3576 会话的 D-Bus 激活环境缺少 WAYLAND_DISPLAY/DISPLAY, 服务会以
    # "Failed to open display" 直接退出 (调用方看到 NoReply / ChildExited)。
    # 由我们主动把这些变量推入激活环境, 服务才能真正起来。
    env = {k: os.environ[k] for k in ("WAYLAND_DISPLAY", "DISPLAY", "XAUTHORITY")
           if os.environ.get(k)}
    if not env:
        return
    try:
        bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus",
                      "org.freedesktop.DBus", "UpdateActivationEnvironment",
                      GLib.Variant("(a{ss})", (env,)), None,
                      Gio.DBusCallFlags.NONE, -1, None)
    except Exception as e:  # 推送失败不致命: 有的会话激活环境本来就是对的
        print("push activation env failed: %s" % e, file=sys.stderr)


_push_activation_env()

opts = GLib.Variant("a{sv}", {"framerate": GLib.Variant("i", 30)})
params = GLib.Variant.new_tuple(GLib.Variant("s", out), opts)
ok, used = bus.call_sync(DEST, PATH, IFACE, "Screencast",
                         params, None, Gio.DBusCallFlags.NONE, -1, None).unpack()
if not ok:
    sys.exit("录屏启动失败: GNOME Shell 拒绝了 Screencast 请求")

time.sleep(secs)
bus.call_sync(DEST, PATH, IFACE, "StopScreencast",
              None, None, Gio.DBusCallFlags.NONE, -1, None)
time.sleep(1)

if not os.path.exists(used) or os.path.getsize(used) == 0:
    sys.exit("录屏失败: 未生成有效文件 (%s)" % used)
print(used)
"""

    def screen_record(self, duration: str = "10") -> Task:
        """录屏: 录制设备屏幕为 mp4, 保存到设备操作所在的机器 (~/qpi_sdk_logs/recordings)。

        Android 设备有 screenrecord 时直接用它; Debian/GNOME (Wayland) SDK 上该命令
        不存在, 改走 GNOME Shell 的 Screencast 接口 (需要图形桌面会话在运行)。
        """
        try:
            secs = int(float(str(duration).strip() or "10"))
        except ValueError:
            raise ValueError(L("录制时长无效: {duration} (请填秒数, 如 10)", duration=repr(duration)))
        if secs < 1:
            raise ValueError(L("录制时长至少 1 秒"))
        adb = self._adb_cmd()
        b64 = base64.b64encode(self._SCREEN_RECORD_PY.encode()).decode()
        # 设备侧统一落到 /data/local/tmp/qpi_rec_out.<ext>, 并把实际路径写进 .path:
        # GNOME 的编码管线可能是 h264/mp4, 也可能是 vp8/webm, 不能写死扩展名。
        dev_script = f"""\
mkdir -p /data/local/tmp && rm -f /data/local/tmp/qpi_rec_out.*
if command -v screenrecord >/dev/null 2>&1; then
  out=/data/local/tmp/qpi_rec_out.mp4
  echo "[QPi] 使用 screenrecord 录屏 {secs}s ..."
  screenrecord --time-limit {secs} "$out"
else
  command -v python3 >/dev/null 2>&1 || {{ echo "[QPi] 设备无 python3, 无法录屏" >&2; exit 1; }}
  U=$(stat -c %U /run/user/*/wayland-0 2>/dev/null | head -n1)
  [ -n "$U" ] || {{ echo "[QPi] 未找到图形桌面会话 (需要 GNOME/Wayland 桌面)" >&2; exit 1; }}
  N=$(id -u "$U")
  # M2/RK3576 的 D-Bus 激活环境缺 WAYLAND_DISPLAY/DISPLAY, 会让 Screencast
  # 服务 (gjs 启动时 Gtk.init) 直接退出; 从会话环境读真实值交给助手推送。
  SENV=$(runuser -u "$U" -- env XDG_RUNTIME_DIR=/run/user/$N systemctl --user show-environment 2>/dev/null)
  WD=$(printf '%s\\n' "$SENV" | sed -n 's/^WAYLAND_DISPLAY=//p' | head -n1)
  XD=$(printf '%s\\n' "$SENV" | sed -n 's/^DISPLAY=//p' | head -n1)
  [ -n "$WD" ] || WD=wayland-0
  [ -n "$XD" ] || XD=:0
  echo "[QPi] 录屏 {secs}s (会话用户: $U) ..."
  rm -rf /tmp/qpi_rec
  rec=$(runuser -u "$U" -- env XDG_RUNTIME_DIR=/run/user/$N \\
        DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$N/bus \\
        WAYLAND_DISPLAY=$WD DISPLAY=$XD \\
        python3 /tmp/qpi_screen_record.py /tmp/qpi_rec/qpi_record {secs})
  # 录制失败时不写 .path (值为空), 由主机侧据此判定失败并给出提示。
  out=""
  if [ -n "$rec" ] && [ -s "$rec" ]; then
    out=/data/local/tmp/qpi_rec_out.${{rec##*.}}
    cp "$rec" "$out"
  fi
fi
echo "$out" > /data/local/tmp/qpi_rec_out.path
if [ -n "$out" ]; then ls -l "$out"; fi
"""
        if self.conn.device_is_remote:
            out_dir = "$HOME/qpi_sdk_logs/recordings"
            pull_dir = out_dir
        else:
            rec_dir = os.path.join(os.path.expanduser("~"), "qpi_sdk_logs", "recordings")
            os.makedirs(rec_dir, exist_ok=True)
            out_dir = shlex.quote(rec_dir)          # bash 侧 (mkdir/校验) 用 WSL 路径
            pull_dir = shlex.quote(self._adb_local_arg(rec_dir))  # adb 侧用可读写路径
        # adb shell 不一定回传设备侧退出码, 故主机侧按「拉回文件是否非空」判定成败。
        # destwin 由 dest 取 basename 派生, 避免 $() 二次求值产生不同的时间戳文件名。
        cmd = (L("mkdir -p {out_dir} && {helper}{adb} shell {dev_script}; dev_out=$({adb} shell cat /data/local/tmp/qpi_rec_out.path 2>/dev/null | tr -d '\\r' | grep '^/' | head -n1); if [ -n \"$dev_out\" ]; then dest={out_dir}/qpi_record_$(date +%Y%m%d_%H%M%S).${dev_out##*.}; destwin={pull_dir}/$(basename \"$dest\"); {adb} pull \"$dev_out\" \"$destwin\"; if [ -s \"$dest\" ]; then echo \"录屏已保存: $dest\"; else rm -f \"$dest\"; echo '[QPi] 录屏失败: 录制文件为空' >&2; exit 1; fi; else echo '[QPi] 录屏失败: 未取到录制文件 (需要已登录的 GNOME/Wayland 桌面会话)' >&2; exit 1; fi", out_dir=out_dir, helper=self._helper_write_cmd(adb, b64), adb=adb, pull_dir=pull_dir, dev_script=shlex.quote(dev_script)))
        return self.executor.exec_device(cmd) if self.conn.device_is_remote \
            else self.executor.exec_local(cmd)

    # ================= 性能监控 =================
    # 各平台的性能节点并不统一 (H1/QCS6490 的 GPU 是 /sys/class/devfreq/3d00000.gpu,
    # M2/RK3576 是 /sys/kernel/gpu/*), 所以这里全部用 glob + 名称过滤在运行期探测,
    # 不按 SDK 硬编码路径; 探测不到就返回 None, 由前端显示「无数据」。
    # 这样同一份代码在 H1 (两块同板) 和 M2 (另一块板) 上都能用, 无需换板即改。
    _PERF_SCRIPT = r"""
echo ==MODEL==
cat /proc/device-tree/model 2>/dev/null; echo
echo ==COMPAT==
cat /proc/device-tree/compatible 2>/dev/null | tr "\000" "\012" | head -8
echo ==NCPU==
grep -c ^processor /proc/cpuinfo 2>/dev/null
echo ==STAT==
head -1 /proc/stat 2>/dev/null
echo ==LOAD==
cat /proc/loadavg 2>/dev/null
echo ==POLICY==
for p in /sys/devices/system/cpu/cpufreq/policy*; do
  [ -d "$p" ] || continue
  printf "%s %s %s\n" "${p##*policy}" "$(cat "$p/scaling_cur_freq" 2>/dev/null)" "$(cat "$p/scaling_max_freq" 2>/dev/null)"
done
echo ==COREFREQ==
for c in /sys/devices/system/cpu/cpu[0-9]*/cpufreq; do
  [ -d "$c" ] || continue
  printf "%s %s\n" "$(basename "$(dirname "$c")")" "$(cat "$c/scaling_cur_freq" 2>/dev/null)"
done
echo ==GPUFREQ==
for d in /sys/class/devfreq/*; do
  n=$(cat "$d/name" 2>/dev/null)
  case "$n" in
    *gpu*|*GPU*|*kgsl*|*mali*|*3d*)
      printf "%s %s %s\n" "$n" "$(cat "$d/cur_freq" 2>/dev/null)" "$(cat "$d/max_freq" 2>/dev/null)";;
  esac
done
echo ==GPUFREQALT==
cat /sys/kernel/gpu/gpu_freq 2>/dev/null
echo ==GPUBUSY==
cat /sys/kernel/gpu/gpu_busy 2>/dev/null
echo ==THERM==
for z in /sys/class/thermal/thermal_zone*; do
  [ -f "$z/type" ] || continue
  printf "%s\t%s\t%s\n" "$(basename "$z")" "$(cat "$z/type" 2>/dev/null)" "$(cat "$z/temp" 2>/dev/null)"
done
echo ==FPS==
# 帧率: DRM debugfs 的 crc 帧计数 (Linux 桌面唯一可靠的实时帧率来源)。

fps=""
crc_ok=""
for c in /sys/kernel/debug/dri/*/crtc-*/crc; do
  [ -w "$c/control" ] || continue
  crc_ok=1
  echo auto > "$c/control" 2>/dev/null
  n=$(timeout 1 cat "$c/data" 2>/dev/null | grep -c .)
  echo none > "$c/control" 2>/dev/null
  if [ "${n:-0}" -gt 0 ]; then fps=$n; break; fi
done
[ -n "$fps" ] || [ -z "$crc_ok" ] || fps=0
# 兜底: 静态桌面这一秒 0 帧时, 用活动 CRTC 的标称刷新率代替, 免得面板一直显示
# 0。它取自 drm state 的 mode 行 (形如 mode: "1280x800": 60 83333 ..., 模式名后
# 第一个数是 vrefresh), 只代表"显示器每秒扫描多少次", 不代表渲染帧率。
if [ "$fps" = 0 ]; then
  for st in /sys/kernel/debug/dri/*/state; do
    [ -r "$st" ] || continue
    ref=$(awk '
      /^[^[:space:]]/ { act=0 }
      /^[[:space:]]*active=1/ { act=1 }
      /^[[:space:]]*mode:/ && act {
        n = split($0, a, ": ")
        if (n >= 3) { split(a[n], b, " "); print b[1]; exit }
      }' "$st" 2>/dev/null)
    [ -n "$ref" ] && { fps=$ref; break; }
  done
fi
echo "$fps"
echo ==END==
"""

    def perf_sample(self) -> dict:
        """采集一次性能快照 (CPU 占用率/频率/负载、GPU 频率/占用、温度、帧率)。

        只读诊断: 单次 adb shell 脚本分段采集, 本机解析成分组字典。CPU 占用率靠
        相邻两次 /proc/stat 的差值计算, 首次调用该字段为 None (前端显示「无数据」)。
        """
        devs = self.adb_devices_parsed()
        dev = next((d for d in devs if d["state"] == "device"), None)
        if dev is None:
            if not devs:
                return {"ok": False, "error": L("未检测到 adb 设备 (请确认设备已连接并授权)")}
            return {"ok": False,
                    "error": L("adb 设备未就绪: {serial} [{state}]", serial=devs[0]['serial'], state=devs[0]['state'])}
        serial = dev["serial"]
        try:
            code, out, err = self.executor.exec_sync_device(
                f"{self._adb_cmd()} -s {shlex.quote(serial)} shell "
                f"{shlex.quote(self._PERF_SCRIPT)}", timeout=30)
        except Exception as e:
            return {"ok": False, "error": L("采集性能数据失败: {e}", e=repr(e))}
        text = out or ""

        # ---- 按 ==NAME== 标记切分段落 ----
        sections: Dict[str, List[str]] = {}
        cur: Optional[str] = None
        for line in text.splitlines():
            line = line.rstrip("\r")
            if line.startswith("==") and line.endswith("==") and len(line) > 4:
                cur = line[2:-2].strip()
                sections.setdefault(cur, [])
                continue
            if cur:
                sections.setdefault(cur, []).append(line)

        def rows(name: str) -> List[str]:
            return [l.strip() for l in sections.get(name, []) if l.strip()]

        info: dict = {"ok": True, "ts": time.time(),
                      "device": {"serial": serial, "state": dev["state"]}}

        # ---- CPU ----
        cpu: dict = {}
        ncpu = rows("NCPU")
        if ncpu:
            try:
                cpu["cores"] = int(ncpu[0])
            except ValueError:
                pass
        stat = rows("STAT")
        if stat:
            parts = stat[0].split()
            if len(parts) > 4 and parts[0] == "cpu":
                try:
                    vals = [float(x) for x in parts[1:]]
                    idle = vals[3] + (vals[4] if len(vals) > 4 else 0.0)
                    total = float(sum(vals))
                    prev = self._perf_prev
                    if prev is not None and total > prev["total"]:
                        busy = 1.0 - (idle - prev["idle"]) / (total - prev["total"])
                        cpu["usagePct"] = round(max(0.0, min(1.0, busy)) * 100.0, 1)
                    self._perf_prev = {"total": total, "idle": idle}
                except ValueError:
                    pass
        load = rows("LOAD")
        if load:
            try:
                cpu["load"] = [float(x) for x in load[0].split()[:3]]
            except ValueError:
                pass
        clusters: List[dict] = []
        for l in rows("POLICY"):
            cols = l.split()
            if len(cols) < 2:
                continue
            cur_f, max_f = _to_mhz(cols[1]), _to_mhz(cols[2] if len(cols) > 2 else "")
            if cur_f is None and max_f is None:
                continue
            clusters.append({"id": cols[0], "curMHz": cur_f, "maxMHz": max_f})
        if clusters:
            cpu["clusters"] = clusters
        cur_freqs = [c["curMHz"] for c in clusters if c["curMHz"] is not None]
        if not cur_freqs:  # 无 policy 目录时退回每核频率
            for l in rows("COREFREQ"):
                cols = l.split()
                if len(cols) >= 2:
                    f = _to_mhz(cols[1])
                    if f is not None:
                        cur_freqs.append(f)
        if cur_freqs:
            cpu["avgMHz"] = round(sum(cur_freqs) / len(cur_freqs), 1)
        max_freqs = [c["maxMHz"] for c in clusters if c["maxMHz"] is not None]
        if max_freqs:
            cpu["maxMHz"] = max(max_freqs)
        if cpu:
            info["cpu"] = cpu

        # ---- GPU (devfreq / RK gpu_freq 单位是 Hz, 与 cpufreq 的 kHz 不同) ----
        gpu: dict = {}
        gpu_rows = rows("GPUFREQ")
        if gpu_rows:
            cols = gpu_rows[0].split()
            gpu["name"] = cols[0]
            if len(cols) > 1:
                gpu["curMHz"] = _hz_to_mhz(cols[1])
            if len(cols) > 2:
                gpu["maxMHz"] = _hz_to_mhz(cols[2])
            gpu = {k: v for k, v in gpu.items() if v is not None}
        if gpu.get("curMHz") is None:  # RK 平台: /sys/kernel/gpu/gpu_freq
            alt = rows("GPUFREQALT")
            if alt:
                f = _hz_to_mhz(alt[0])
                if f is not None:
                    gpu["curMHz"] = f
        busy = rows("GPUBUSY")
        if busy:
            try:
                gpu["busyPct"] = round(max(0.0, min(100.0, float(busy[0].split()[0]))), 1)
            except (IndexError, ValueError):
                pass
        if gpu:
            info["gpu"] = gpu

        # ---- 温度 (只取真温度通道; 电池电压/电流/限流档位与无效通道跳过) ----
        thermal: dict = {}
        peak: Optional[float] = None
        for l in rows("THERM"):
            cols = l.split("\t")
            if len(cols) < 3:
                continue
            name = cols[1] or cols[0]
            try:
                raw = int(cols[2])
            except ValueError:
                continue
            if name == "vbat" or "ibat-lvl" in name or "bcl-lvl" in name or "bat" in name:
                continue
            if raw <= -40000:  # 无有效热敏源, 读数停在量程底端
                continue
            celsius = raw / 1000.0
            low = name.lower()
            if "cpu" in low:
                key = "cpuTempC"
            elif "gpu" in low:
                key = "gpuTempC"
            elif "ddr" in low:
                key = "ddrTempC"
            elif "aoss" in low or "soc" in low:
                key = "socTempC"
            else:
                key = None
            if key is not None and (key not in thermal or celsius > thermal[key]):
                thermal[key] = round(celsius, 1)
            if peak is None or celsius > peak:
                peak = celsius
        if peak is not None:
            thermal["maxTempC"] = round(peak, 1)
        if thermal:
            info["thermal"] = thermal

        # ---- 帧率 ----
        # 0 是有效值 (桌面空闲这一秒没有绘制), 只有读不到 (内核不支持 CRC) 才留空。
        # 取第一条能解析成整数的行: 设备端 shell 可能往该段落里混入警告文本。
        for line in rows("FPS"):
            try:
                fps = int(line.split()[0])
            except (IndexError, ValueError):
                continue
            if fps >= 0:
                info["fps"] = fps
            break

        # ---- 平台 (仅用于展示板名; 采集逻辑不依赖它) ----
        # 优先按**设备自身**的 model/compatible 识别真实板卡: 当连接的 SDK 与实际
        # 插入的板子型号不一致时 (如连 H1 SDK 插 M2 板), 显示以实际板子为准;
        # 设备端识别不出 (无 /proc/device-tree) 时才回退到 SDK 声明的平台。
        try:
            plat = _platform_from_device(
                rows("MODEL")[0] if rows("MODEL") else "", rows("COMPAT"))
            if plat is None:
                sdk_plat = self.platform()
                if sdk_plat.get("board") or sdk_plat.get("soc"):
                    plat = {"board": sdk_plat.get("board"),
                            "soc": sdk_plat.get("soc"),
                            "family": sdk_plat.get("family")}
            if plat:
                info["platform"] = plat
        except Exception:
            pass
        return info

    # ================= SKILL 文档 =================
    def list_skills(self) -> List[dict]:
        """列出 skills/ 下的 SKILL 文档 (md/skill 文件)。"""
        skills_dir = self._abs("skills")
        # 一次 exec 同时拿 路径+大小, 避免每文件一次 stat 往返
        code, out, err = self.executor.exec_sync(
            f"find {shlex.quote(skills_dir)} -maxdepth 3 -type f "
            f"\\( -iname '*.md' -o -iname 'SKILL.md' \\) "
            f"-exec stat -c '%n|%s' {{}} \\; 2>/dev/null", timeout=20)
        out_list = []
        for line in out.splitlines():
            line = line.strip()
            if not line or "|" not in line:
                continue
            f, _, size = line.rpartition("|")
            try:
                out_list.append({"name": self.conn.basename(f),
                                 "path": f, "size": int(size or 0)})
            except Exception:
                continue
        return out_list

    def read_skill(self, name: str) -> str:
        skills_dir = self._abs("skills")
        # 先精确匹配, 再模糊查找
        for cand in (self.conn.join(skills_dir, name),
                     self.conn.join(skills_dir, name, "SKILL.md"),
                     self.conn.join(skills_dir, name + ".md")):
            if self._exists(cand):
                return self.executor.read_file(cand)
        code, out, err = self.executor.exec_sync(
            f"find {shlex.quote(skills_dir)} -iname '*{name}*' -type f 2>/dev/null | head -1",
            timeout=20)
        if out.strip():
            return self.executor.read_file(out.strip())
        return L("(未找到 SKILL 文档: {name})", name=name)

    # ================= SDK 版本 (git 状态) =================
    # git log 字段分隔符 (US): 提交说明里不会出现, 无需转义
    _GIT_SEP = "\x1f"
    # 分段标记: 一次 exec_sync 往返取齐所有字段, 避免 SSH 下多次 RTT
    _GIT_MARK = "@@QPI@@"

    def git_info(self, fetch: bool = True, log_limit: int = 20) -> dict:
        """读取 SDK 目录的 git 状态: 分支 / 当前提交 / 落后远端提交数 / 近期提交。

        fetch=True 时先联网 `git fetch` 刷新远端引用 (失败不报错, 只把 fetch_ok 置
        False, 再用本地已有的远端引用计算), 供"连接 SDK 后自动检查是否最新"使用。
        非 git 仓库返回 is_repo=False, 不抛异常。
        """
        info: Dict[str, object] = {
            "ok": False, "is_repo": False, "error": "",
            "sdk_root": self.conn.sdk_abs,
            "branch": "", "commit": "", "commit_full": "", "subject": "",
            "author": "", "date": "",
            "remote": "", "upstream": "", "has_upstream": False,
            "behind": 0, "ahead": 0, "up_to_date": False,
            "latest_remote": None, "log": [],
            "fetch_ok": False, "shallow": False,
        }
        sdk = self.conn.sdk_abs
        if not sdk:
            info["error"] = L("未配置 SDK 目录")
            return info
        q = shlex.quote(sdk)

        def run(cmd: str, timeout: float = 30) -> tuple:
            """执行 git 命令。目录不存在 / 无 git 等异常统一吞掉并记入 error,
            由调用方按退出码与输出判定 —— 探测类接口不应把异常抛给前端。
            """
            try:
                return self.executor.exec_sync(self._net_prelude() + cmd, timeout=timeout)
            except Exception as e:  # noqa: BLE001
                info["error"] = str(e)
                return -1, "", str(e)
        # 注意: git 的 %h/%H/%an/%ad/%s 是 git 自己的占位符, 不能走 Python 的
        # `%` 格式化 (%h 不是合法转换符, 会直接抛 ValueError), 用 f-string 拼接。
        s = self._GIT_SEP
        fmt = f"%h{s}%H{s}%an{s}%ad{s}%s"

        def part(args: str) -> str:
            return (f"git -C {q} {args} 2>/dev/null; "
                    f"printf '\\n{self._GIT_MARK}\\n'")

        script = "; ".join([
            part("rev-parse --is-inside-work-tree"),
            part("rev-parse --abbrev-ref HEAD"),
            part("config --get remote.origin.url"),
            part("rev-parse --abbrev-ref --symbolic-full-name '@{u}'"),
            part(f"log -n {int(log_limit)} --pretty=format:'{fmt}' --date=short"),
            part("rev-parse --is-shallow-repository"),
        ])
        _code, out, _err = run(script, timeout=45)
        sections = (out or "").split(self._GIT_MARK)

        def sec(i: int) -> str:
            return sections[i].strip() if i < len(sections) else ""

        if sec(0) != "true":
            # error 可能已被 run() 写入 (目录不存在 / git 不可用), 此时保留原始原因
            info["error"] = info["error"] or L("SDK 目录不是 git 仓库")
            return info
        info["is_repo"] = True
        info["ok"] = True
        info["branch"] = sec(1)
        info["remote"] = sec(2)
        info["upstream"] = sec(3)
        info["shallow"] = sec(5) == "true"

        commits = _parse_git_log(sec(4), self._GIT_SEP)
        info["log"] = commits
        if commits:
            head = commits[0]
            info["commit"] = head["hash"]
            info["commit_full"] = head["full"]
            info["subject"] = head["subject"]
            info["author"] = head["author"]
            info["date"] = head["date"]

        # 远端比较基准: 优先跟踪分支, 其次 origin/<当前分支>
        ref = str(info["upstream"] or "")
        if not ref and info["branch"]:
            ref = f"origin/{info['branch']}"
        if not ref:
            info["error"] = L("当前分支没有远端跟踪分支, 无法比较版本")
            return info
        if fetch:
            fcode, _fo, _fe = run(
                f"git -C {q} fetch --quiet --no-tags origin", timeout=90)
            info["fetch_ok"] = fcode == 0
        # 远端引用必须真实存在 (浅克隆/未 fetch 过时可能缺失)
        _vc, vout, _ve = run(
            f"git -C {q} rev-parse --verify --quiet {shlex.quote(ref)}", timeout=20)
        if _vc != 0 or not (vout or "").strip():
            info["error"] = L("找不到远端跟踪分支 {ref} (请先确认网络或手动 git fetch)", ref=ref)
            return info
        info["has_upstream"] = True

        def count(rng: str) -> int:
            _c, o, _e = run(
                f"git -C {q} rev-list --count {rng}", timeout=30)
            try:
                return int((o or "0").strip() or 0)
            except ValueError:
                return 0

        info["behind"] = count(f"HEAD..{ref}")
        info["ahead"] = count(f"{ref}..HEAD")
        info["up_to_date"] = info["behind"] == 0

        _lc, lout, _le = run(
            f"git -C {q} log -n 1 --pretty=format:'{fmt}' --date=short "
            f"{shlex.quote(ref)}", timeout=30)
        latest = _parse_git_log(lout, self._GIT_SEP)
        info["latest_remote"] = latest[0] if latest else None
        return info

    # ================= 仓库引导 (克隆) =================
    def repo_status(self, repos: Optional[dict] = None) -> List[dict]:
        """组件缺失检测。repos: {组件名: git_url 或 本地路径}。"""
        components = [
            (L("核心 SDK (build.sh + 工具链)"), "build.sh"),
            (L("内核源码 kernel/"), "kernel"),
            (L("构建工具 tools/"), "tools"),
            (L("预编译资源 prebuilds/"), "prebuilds"),
            (L("交叉工具链 toolchains/"), "toolchains"),
            (L("工程模板 docs/templates/"), "docs/templates"),
            (L("SKILL 文档 skills/"), "skills"),
            (L("设备树 Overlay overlay/"), "overlay"),
        ]
        # 合并为一次命令, 避免 WSL/ssh 下逐项串行往返
        checks = "; ".join(
            f'[ -e {shlex.quote(self._abs(rel))} ] && echo "{rel}=1" || echo "{rel}=0"'
            for _, rel in components)
        code, out, err = self.executor.exec_sync(checks, timeout=30)
        exists: Dict[str, bool] = {}
        for line in out.splitlines():
            line = line.strip()
            if not line or "=" not in line:
                continue
            rel, _, mark = line.partition("=")
            exists[rel] = mark == "1"
        out_list = []
        for label, rel in components:
            out_list.append({"label": label, "rel": rel,
                             "exists": exists.get(rel, False),
                             "url": (repos or {}).get(rel, "")})
        return out_list

    def clone_component(self, rel: str, url: str, force: bool = False) -> Task:
        """git clone 组件到 SDK 根目录 (url 可为 git/https/本地路径)。"""
        if not url:
            raise ValueError(L("组件 {rel} 未配置仓库地址", rel=rel))
        dst = self._abs(rel)
        if self._exists(dst):
            if not force:
                raise ValueError(L("{rel} 已存在, 如需覆盖请勾选强制", rel=rel))
            self.executor.exec_sync(f"rm -rf {shlex.quote(dst)}", timeout=60)
        # 父目录必须存在 (核心 SDK 由 build.sh 判断; 组件直接放根)
        if rel != "build.sh":
            self.executor.exec_sync(
                f"mkdir -p {shlex.quote(self.conn.dirname(dst))}", timeout=15)
        cmd = f"cd {shlex.quote(self.conn.sdk_abs)} && git clone {shlex.quote(url)} {shlex.quote(rel)}"
        return self.executor.exec(self._net_prelude() + cmd)

    def clone_core(self, url: str, target_dir: str) -> Task:
        """克隆核心 SDK 到指定目录 (sdk_root 为空时用)。"""
        if not url:
            raise ValueError(L("未配置核心 SDK 仓库地址"))
        if not target_dir:
            raise ValueError(L("请指定克隆目标目录"))
        cmd = f"git clone {shlex.quote(url)} {shlex.quote(target_dir)}"
        return self.executor.exec(self._net_prelude() + cmd)

    def download_sdk(self, version: str, repo: str = "", branch: str = "",
                     archive: str = "", target_dir: str = "") -> Task:
        r"""下载 SDK 到本地目录 (版本号小写作子目录名)。

        始终走 exec_local (本机执行, 不依赖连接模式):
        - repo 优先: git clone --depth 1 [--branch x] --progress
        - archive 其次: curl 拉取后 unzip/tar 解压
        保存目录为 WSL 共享路径 (\\wsl.localhost\... ) 时, 自动改用 wsl.exe
        在发行版内执行 git/curl/解压, 避免 Windows 工具写 9P 共享路径失败。
        """
        if not target_dir:
            raise ValueError(L("未指定保存目录"))
        target_dir = os.path.expanduser(target_dir.strip().strip('"'))
        if not os.path.isabs(target_dir):
            raise ValueError(L("保存目录必须是绝对路径: {path}", path=target_dir))
        wsl = _parse_wsl_unc(target_dir)
        if wsl:
            return self._download_sdk_wsl(version, repo, branch, archive, wsl)
        # 本机普通路径: 直接 Windows git/curl
        os.makedirs(target_dir, exist_ok=True)
        target = os.path.join(target_dir, version.lower())
        if os.path.exists(target):
            raise ValueError(L("目标目录已存在: {target}\n请删除后重试, 或换一个保存目录", target=target))
        if repo:
            args = ["git", "clone", "--depth", "1"]
            if branch:
                args += ["--branch", branch]
            args += ["--progress", repo, target]
            return self.executor.exec_local(
                self._net_prelude() + " ".join(shlex.quote(a) for a in args))
        if archive:
            fname = archive.split("?")[0].split("/")[-1] or "sdk"
            local = os.path.join(target_dir, fname)
            cmds = [f"curl -L -o {shlex.quote(local)} {shlex.quote(archive)}",
                    f"mkdir -p {shlex.quote(target)}"]
            if fname.endswith(".zip"):
                cmds.append(f"unzip -o {shlex.quote(local)} -d {shlex.quote(target)}")
            else:
                cmds.append(f"tar -xf {shlex.quote(local)} -C {shlex.quote(target)}")
            return self.executor.exec_local(self._net_prelude() + " && ".join(cmds))
        raise ValueError(L("未配置下载源 (repo 或 archive 均为空)"))

    def _download_sdk_wsl(self, version: str, repo: str, branch: str,
                          archive: str, wsl: tuple) -> Task:
        """保存目录是 WSL 共享路径: 在发行版内用 git/curl 下载 (wsl.exe 执行)。

        wsl = (发行版名, linux 根路径)。目标 = <linux根>/<版本小写>。
        关闭任务终端即终止 (与 exec_local 一致)。
        """
        distro, linux_root = wsl
        linux_target = posixpath.join(linux_root, version.lower())
        # 先确认目标不存在 (WSL 内检查, 避免重复下载)
        check = (f"test -e {shlex.quote(linux_target)} && echo EXISTS")
        code, out, err = self.executor.exec_sync_local(
            f"wsl.exe -d {shlex.quote(distro)} -- bash -lc {shlex.quote(check)}",
            timeout=30)
        if "EXISTS" in out:
            raise ValueError(
                L("目标目录已存在: {target}\n请删除后重试, 或换一个保存目录", target=linux_target))
        if repo:
            args = ["git", "clone", "--depth", "1"]
            if branch:
                args += ["--branch", branch]
            args += ["--progress", repo, linux_target]
            cmd = f"mkdir -p {shlex.quote(linux_root)} && " \
                  + " ".join(shlex.quote(a) for a in args)
        elif archive:
            fname = archive.split("?")[0].split("/")[-1] or "sdk"
            linux_file = posixpath.join(linux_root, fname)
            cmds = [f"mkdir -p {shlex.quote(linux_root)}",
                    f"curl -L -o {shlex.quote(linux_file)} {shlex.quote(archive)}",
                    f"mkdir -p {shlex.quote(linux_target)}"]
            if fname.endswith(".zip"):
                cmds.append(f"unzip -o {shlex.quote(linux_file)} -d {shlex.quote(linux_target)}")
            else:
                cmds.append(f"tar -xf {shlex.quote(linux_file)} -C {shlex.quote(linux_target)}")
            cmd = " && ".join(cmds)
        else:
            raise ValueError(L("未配置下载源 (repo 或 archive 均为空)"))
        # 在发行版内执行 (--cd / 避免根目录不存在时 --cd 失败; mkdir -p 已建好父目录)
        return self.executor.exec_local(
            f"wsl.exe -d {shlex.quote(distro)} --cd / -- bash -lc {shlex.quote(self._net_prelude() + cmd)}")

    # ================= 设备树 (DTS/DTSI 配置) =================
    def _board_main_dts(self, base: str, kernel_dts: str = "") -> str:
        """确定板级主 .dts 路径: VERSION.txt 的 KERNEL_DTS 优先, 其次 M2 固定名。

        都没有时兜底扫描厂商目录, 按关键字 (quectel/idp-pi/-pi/board) 挑选。
        """
        names: List[str] = []
        if kernel_dts:
            k = kernel_dts.strip()
            if k.endswith(".dts"):
                k = k[:-4]
            names.append(k)
        names.append("rk3576-quectel-pi-m2-linux")
        for name in names:
            p = self.conn.join(base, name + ".dts")
            if self._exists(p):
                return p
        try:
            code, out, err = self.executor.exec_sync(
                f"ls {shlex.quote(base)}/*.dts 2>/dev/null", timeout=15)
        except Exception:
            return ""
        cands = [l.strip() for l in out.splitlines() if l.strip().endswith(".dts")]
        for kw in ("quectel", "idp-pi", "-pi", "board"):
            for c in cands:
                if kw in self.conn.basename(c).lower():
                    return c
        return cands[0] if cands else ""

    def _board_dtb_path(self) -> str:
        """定位板级主 .dts 对应的编译产物 .dtb 路径 (与 .dts 同目录同名)。"""
        plat = self.platform()
        family = str(plat.get("family") or "rockchip")
        vendor_dir = "qcom" if family == "qualcomm" else "rockchip"
        base = self._abs(f"kernel/arch/arm64/boot/dts/{vendor_dir}")
        names: List[str] = []
        if plat.get("kernel_dts"):
            k = str(plat["kernel_dts"]).strip()
            if k.endswith(".dts"):
                k = k[:-4]
            names.append(k)
        names.append("rk3576-quectel-pi-m2-linux")
        for name in names:
            p = self.conn.join(base, name + ".dtb")
            if self._exists(p):
                return p
        return ""

    def _board_sources(self) -> List[dict]:
        """定位板级设备树 DTS/DTSI 源文件列表 (可编辑源)。

        H1 (Qualcomm/QCS) 与 M2 (Rockchip/RK) 通用:
        - 厂商目录按平台自动选择: qualcomm -> dts/qcom, 其余 -> dts/rockchip
        - 主 .dts 优先取 VERSION.txt 的 KERNEL_DTS (两套 SDK 都有声明),
          缺失时回退 M2 固定名, 再兜底目录扫描
        - 从主 .dts 按 #include 文本展开顺序做 DFS: 被包含文件在前,
          包含者在后 —— 与 DTS 预处理器语义一致, "后出现的 &label 块覆盖
          先出现的同名块"
        - 只收集"板级"文件: 主 .dts 本身 + 文件名含 厂商关键字 (如 quectel) /
          "addons" / 主文件板型 token (如 idp-pi) 的 .dtsi (SoC/PMIC 基座不收集)
        每个条目: {id, kind, label, path}。
        """
        plat = self.platform()
        family = str(plat.get("family") or "rockchip")
        vendor_dir = "qcom" if family == "qualcomm" else "rockchip"
        base = self._abs(f"kernel/arch/arm64/boot/dts/{vendor_dir}")

        main_dts = self._board_main_dts(base, str(plat.get("kernel_dts") or ""))
        files: List[str] = []
        seen: set = set()
        # 板级判定关键字: 厂商名 (quectel) + 通用 addons + 主文件板型 token
        board_tokens = {"addons"}
        if plat.get("vendor"):
            board_tokens.add(str(plat["vendor"]).lower())
        if main_dts:
            stem = self.conn.basename(main_dts)
            if stem.endswith(".dts"):
                stem = stem[:-4]
            parts = stem.split("-")
            if len(parts) >= 2:
                board_tokens.add("-".join(parts[-2:]))
            board_tokens.add(stem)

        def walk(f: str) -> None:
            if f in seen:
                return
            seen.add(f)
            try:
                text = self._read_dts(f)
            except Exception:
                return
            dirname = self.conn.dirname(f)
            for m in re.finditer(r'#include\s+"([^"]+)"', text):
                inc = m.group(1)
                if inc.endswith(".dtsi"):
                    walk(self.conn.join(dirname, inc))
            if f == main_dts:
                files.append(f)
                return
            base_name = self.conn.basename(f).lower()
            if base_name.endswith(".dtsi") and any(
                    t and t in base_name for t in board_tokens):
                files.append(f)

        if main_dts and self._exists(main_dts):
            walk(main_dts)
        if not files:
            # 兜底: 直接扫厂商目录下匹配板级关键字的 dtsi
            pats = " ".join(shlex.quote(f"{base}/{t}*.dtsi")
                            for t in sorted(board_tokens) if t)
            if pats:
                code, out, err = self.executor.exec_sync(
                    f"ls {pats} 2>/dev/null", timeout=15)
                files = [f.strip() for f in out.splitlines() if f.strip()]
        sources: List[dict] = []
        for f in files:
            base_name = self.conn.basename(f)
            sources.append({
                "id": base_name,
                "kind": "board-dtsi" if base_name.endswith(".dtsi") else "board-dts",
                "label": base_name,
                "path": f,
            })
        return sources

    def _parse_source_nodes(self, src: dict) -> List[dict]:
        """解析一个板级源文件中的 &label 覆盖块, 返回可配置节点列表。"""
        try:
            content = self._read_dts(src["path"])
        except Exception:
            return []
        nodes: List[dict] = []
        for b in _find_dts_blocks(content):
            body = content[b["body_start"]:b["end"]]
            props = _dts_block_props(body)
            status = props.get("status", "")
            # _dts_block_props 保留原始值含引号 (如 '"disabled"'), 去除引号再判定
            enabled = status.strip().strip('"') != "disabled"
            nodes.append({
                "name": b["label"],
                "enabled": enabled,
                "status": status,
                "properties": props,
                "path": "",
                "source": src["id"],
                "sourceLabel": b["label"],
            })
        nodes.sort(key=lambda n: n["name"])
        return nodes

    def device_tree_nodes(self) -> dict:
        """解析板级设备树 DTS/DTSI, 返回可配置节点。

        结构: {path, sources:[{id, kind, label, nodes:[...]}]}
        - 聚合视图 compiled-dtb: 全量外设 (通过 source/sourceLabel 回链可编辑源)
        - 其余为各板级源文件, 可直接改写。
        """
        sources = self._board_sources()
        if not sources:
            raise ValueError(L("未找到板级设备树源文件 (请确认 SDK 内核含板级 dts/dtsi)"))
        all_nodes: List[dict] = []
        src_views: List[dict] = []
        for s in sources:
            nodes = self._parse_source_nodes(s)
            src_views.append({
                "id": s["id"], "kind": s["kind"], "label": s["label"],
                "path": s["path"], "nodes": nodes,
            })
            all_nodes.extend(nodes)
        dtb_path = self._board_dtb_path()
        path = dtb_path if dtb_path else (sources[0]["path"] if sources else "")
        # compiled-dtb 合并视图: 同名 &label 块跨文件合并 (后包含者覆盖先包含者),
        # 属性取并集 (后出现者覆盖), 每个标签只保留一条, 回链到"胜出"的源。
        merged: Dict[str, dict] = {}
        for n in all_nodes:
            lab = n["name"]
            if lab in merged:
                prev = merged[lab]
                merged[lab] = {
                    **prev,
                    "properties": {**prev["properties"], **n["properties"]},
                    "status": n["status"],
                    "enabled": n["enabled"],
                    "source": n["source"],
                    "sourceLabel": n["sourceLabel"],
                    "path": n["path"],
                }
            else:
                merged[lab] = dict(n)
        # soc 显示名以主 .dts 文件名为准 (H1: QCS6490, M2: RK3576)
        main_entry = next((s for s in sources if s["kind"] == "board-dts"),
                          sources[0] if sources else None)
        soc = main_entry["label"].split("-")[0].upper() if main_entry else "RK3576"
        dtb_view = {
            "id": "compiled-dtb",
            "kind": "compiled-dtb",
            "soc": soc,
            "label": "compiled-dtb",
            "nodes": [merged[k] for k in sorted(merged)],
        }
        return {"path": path, "sources": [dtb_view] + src_views}

    def _resolve_source(self, source: str, node: str) -> dict:
        """解析可编辑源: 优先按 source id 匹配; 否则跨源按节点名 (&label) 定位。

        跨源定位取最后一个匹配文件 —— 与 DTS "后包含覆盖先包含" 语义一致
        (例如 can0 同时出现在 common.dtsi 与 m2-linux.dtsi 时, 后者胜出)。
        """
        sources = self._board_sources()
        if source and source != "compiled-dtb":
            for s in sources:
                if s["id"] == source:
                    return s
        found = None
        for s in sources:
            try:
                content = self._read_dts(s["path"])
            except Exception:
                continue
            if any(b["label"] == node for b in _find_dts_blocks(content)):
                found = s
        if found is None:
            raise ValueError(
                L("未找到可编辑的板级源文件: 节点 &{node} (源 {source})", node=node, source=source or L('未指定')))
        return found

    def _backup_dts(self, path: str) -> None:
        """写前备份到同目录 .bak (幂等覆盖), 便于回滚。"""
        try:
            content = self.executor.read_file(path)
            self.executor.write_file(path + ".bak", content)
        except Exception:
            pass

    def set_device_tree_status(self, node: str, enabled: bool,
                               source: str = "") -> dict:
        """设置节点 status 属性 (okay/disabled) 并回写 DTS 源文件。"""
        src = self._resolve_source(source, node)
        dts_path = src["path"]
        content = self.executor.read_file(dts_path)
        target = next((b for b in _find_dts_blocks(content)
                       if b["label"] == node), None)
        if target is None:
            raise ValueError(L("未在 {label} 中找到节点 &{node}", label=src['label'], node=node))
        new_status = "okay" if enabled else "disabled"
        body = content[target["body_start"]:target["end"]]
        if re.search(r'(?m)^\s*status\s*=\s*"(okay|disabled)";', body):
            body_new = re.sub(
                r'(?m)^(\s*status\s*=\s*)"(okay|disabled)";',
                rf'\g<1>"{new_status}";', body, count=1)
        else:
            indent = _dts_block_base_indent(body)
            body_new = (body.rstrip("\n") + "\n"
                        + f'{indent}status = "{new_status}";' + "\n")
        self._backup_dts(dts_path)
        new_content = (content[:target["body_start"]]
                       + body_new + content[target["end"]:])
        self.executor.write_file(dts_path, new_content)
        self._invalidate_dts(dts_path)
        return {"ok": True, "node": node, "enabled": enabled,
                "source": src["id"]}

    def set_device_tree_properties(self, node: str, source: str,
                                   properties: str) -> dict:
        """替换节点顶层属性 (仅接受顶层属性声明) 并回写 DTS 源文件。"""
        src = self._resolve_source(source, node)
        dts_path = src["path"]
        content = self.executor.read_file(dts_path)
        props = _parse_props_text(properties)
        self._backup_dts(dts_path)
        new_content = _rewrite_block_props(content, node, props)
        self.executor.write_file(dts_path, new_content)
        self._invalidate_dts(dts_path)
        return {"ok": True, "node": node, "source": src["id"]}

    def resolve_device_tree_source(self, node: str, label: str = "") -> dict:
        """定位节点所在的板级源文件 (供前端跳转/定位可编辑源)。"""
        src = self._resolve_source(label or "", node)
        return {"source": src["id"], "node": node}

    # ================= 通用 =================
    def exec_raw(self, cmd: str, cwd: str = "") -> Task:
        return self.executor.exec(cmd, cwd=cwd)

# ================= WSL 宿主识别 (工具转发到 Windows) =================
# 后端自身可能运行在 WSL 内 (VS Code Remote-WSL): 此时 Linux 版烧录工具拿不到 USB
# (WSL2 默认不做 USB 直通), 但 WSL 的 interop 能直接执行 Windows exe —— 由 Windows
# 进程用 Windows USB 栈访问板子。因此 WSL 下改用插件内置的 Windows 工具, 并把镜像
# 路径转成 Windows 可访问形式 (wsl.localhost UNC)。

_WSL_HOST_CACHE: Optional[bool] = None
_WSL_INTEROP_CACHE: Optional[bool] = None
_WSL_MNT_RE = re.compile(r"^/mnt/([a-zA-Z])(?:/(.*))?$")


def _is_wsl_host() -> bool:
    """后端自身是否运行在 WSL 内 (只看进程所处环境, 与连接模式无关)。"""
    global _WSL_HOST_CACHE
    if _WSL_HOST_CACHE is not None:
        return _WSL_HOST_CACHE
    ok = False
    if sys.platform.startswith("linux"):
        if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
            ok = True
        else:
            try:
                with open("/proc/version", "r", encoding="utf-8",
                          errors="ignore") as f:
                    ok = "microsoft" in f.read().lower()
            except OSError:
                ok = False
    _WSL_HOST_CACHE = ok
    return ok


def _wsl_interop_ok() -> bool:
    """WSL 是否启用了 Windows interop (即能否直接执行 .exe)。"""
    global _WSL_INTEROP_CACHE
    if _WSL_INTEROP_CACHE is None:
        _WSL_INTEROP_CACHE = os.path.exists("/proc/sys/fs/binfmt_misc/WSLInterop")
    return _WSL_INTEROP_CACHE


def _flash_tool_platform() -> str:
    """烧录工具按哪个平台的版本来选。

    WSL 宿主下返回 "win32": 经 interop 执行 Windows 版 upgrade_tool.exe, 由 Windows
    进程访问 USB —— WSL2 默认不直通 USB, Linux 版工具看不到 rockusb 设备。
    """
    if sys.platform == "linux" and _is_wsl_host() and _wsl_interop_ok():
        return "win32"
    return sys.platform


def _adb_host_kind() -> str:
    """后端宿主类型 (adb 策略分支用): windows / wsl / linux / macos。

    与连接模式无关: 只看插件后端进程跑在哪种系统上。
    """
    if sys.platform == "win32":
        return "windows"
    if _is_wsl_host():
        return "wsl"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


# ================= WSL 串口转发 (Windows COM 口) =================
# WSL2 默认不直通 USB: 插件在 WSL 内只能看到内核虚拟的 /dev/ttyS* (crw------- root:root,
# 普通用户打不开, 也不接任何设备), 真实 USB 串口只存在于 Windows 侧。interop 可用时用
# powershell.exe 的 System.IO.Ports 打开 Windows COM 口, 由 Windows 进程访问串口。
# 仅在 WSL 宿主下生效, Windows / 原生 Linux 行为完全不变 (仍走 pyserial)。
_PS_FALLBACK = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
_SERIAL_FWD_CACHE: Optional[bool] = None
_COM_RE = re.compile(r"^COM\d+$", re.I)


def _parse_git_log(text: str, sep: str = "\x1f") -> List[dict]:
    """解析 `git log --pretty=format:%h<US>%H<US>%an<US>%ad<US>%s` 的输出。

    US (\\x1f) 是 git 推荐的分隔符: 提交说明里几乎不会出现, 无需转义处理。
    """
    out: List[dict] = []
    for line in (text or "").splitlines():
        line = line.rstrip("\r")
        if not line or sep not in line:
            continue
        parts = line.split(sep)
        if len(parts) < 5:
            continue
        out.append({
            "hash": parts[0].strip(),
            "full": parts[1].strip(),
            "author": parts[2].strip(),
            "date": parts[3].strip(),
            "subject": sep.join(parts[4:]).strip(),
        })
    return out


def _wsl_serial_forward() -> bool:
    """是否把本机串口操作 (枚举/AT) 转发到 Windows 执行 (仅 WSL 宿主)。"""
    global _SERIAL_FWD_CACHE
    if _SERIAL_FWD_CACHE is None:
        _SERIAL_FWD_CACHE = (sys.platform == "linux" and _is_wsl_host()
                             and _wsl_interop_ok())
    return _SERIAL_FWD_CACHE


def _ps_command(script: str) -> str:
    """把 PowerShell 脚本包装成可在 WSL 内执行的 shell 命令。

    走 -EncodedCommand (UTF-16LE base64) 而非 -Command/-File: 彻底规避引号、换行、
    中文与 WSL 路径翻译问题; PATH 里没有 powershell.exe 时回退到系统绝对路径。
    """
    import base64
    enc = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return ('PS=powershell.exe; command -v "$PS" >/dev/null 2>&1 || '
            f'PS={_PS_FALLBACK}; '
            f'"$PS" -NoProfile -NonInteractive -EncodedCommand {enc}')


def _ps_quote(s: str) -> str:
    """PowerShell 单引号字符串转义 (内部单引号翻倍)。"""
    return "'" + str(s).replace("'", "''") + "'"


# 枚举 Windows COM 口: 端口名来自 .NET SerialPort, 描述来自 PnP 设备名 (中文名按
# GBK 输出, 由 executor 的 _decode_out 回退解码)。$ProgressPreference 关掉,
# 否则 PowerShell 会把 "正在准备首次使用模块" 之类的进度记录打到 stderr。
_PS_ENUM_PORTS = r"""
$ProgressPreference='SilentlyContinue'
$descs=@{}
try{
  Get-CimInstance Win32_PnPEntity -Filter "Name LIKE '%(COM%'" -ErrorAction SilentlyContinue | ForEach-Object {
    if($_.Name -match '\((COM\d+)\)'){ $descs[$Matches[1]]=($_.Name -replace '\s*\(COM\d+\)\s*','').Trim() }
  }
}catch{}
foreach($n in [System.IO.Ports.SerialPort]::GetPortNames()){
  $d=''; if($descs.ContainsKey($n)){ $d=$descs[$n] }
  Write-Output ($n + "`t" + $d)
}
"""

# 经 Windows COM 口发 AT 指令。响应按原始字节读取后以 UTF-8 输出 (非 UTF-8 则按
# GB18030 回退), 输出格式与 pyserial 通道一致, 前端无需区分。
_PS_AT_TEMPLATE = r"""
$ProgressPreference='SilentlyContinue'
$ErrorActionPreference='Continue'
$port='__PORT__'; $baud=__BAUD__; $cmds=@(__CMDS__)
$out=[Console]::OpenStandardOutput()
function Emit([string]$s){ $b=[System.Text.Encoding]::UTF8.GetBytes($s+"`n"); $out.Write($b,0,$b.Length); $out.Flush() }
function Dec([byte[]]$b){
  $t=[System.Text.Encoding]::UTF8.GetString($b)
  if($t.Contains([char]0xFFFD)){ $t=[System.Text.Encoding]::GetEncoding(936).GetString($b) }
  return $t
}
$sp=New-Object System.IO.Ports.SerialPort($port,$baud,'__PARITY__',__BYTESIZE__,'__STOPBITS__')
$sp.ReadTimeout=200; $sp.WriteTimeout=2000
$sp.Handshake=[System.IO.Ports.Handshake]::__FLOW__
$sp.DtrEnable=$true; $sp.RtsEnable=$true
Emit ("[AT] 打开串口 $port @ $baud __BYTESIZE____PARITY__ __STOPBITS__ 流控=__FLOW__ ...")
try{ $sp.Open() }catch{ Emit ("[AT] 串口操作失败: " + $_.Exception.Message); exit 1 }
Emit '[AT] 串口打开成功'
foreach($c in $cmds){
  Emit ("[AT] >>> 发送: $c")
  try{
    $sp.DiscardInBuffer()
    $sp.Write($c + "`r`n")
    Start-Sleep -Milliseconds 300
    $ms=New-Object System.IO.MemoryStream
    $end=[DateTime]::Now.AddMilliseconds(1800)
    while([DateTime]::Now -lt $end){
      $n=0; try{ $n=$sp.BytesToRead }catch{ $n=0 }
      if($n -gt 0){
        $tmp=New-Object byte[] $n
        $r=$sp.BaseStream.Read($tmp,0,$n)
        if($r -gt 0){ $ms.Write($tmp,0,$r)
          if((Dec $ms.ToArray()) -match '(OK|ERROR)\s*$'){ break } }
      } else { Start-Sleep -Milliseconds 20 }
    }
    $txt=Dec $ms.ToArray()
    if($txt.Length -gt 0){ Emit $txt } else { Emit '(无回复)' }
  }catch{ Emit ("[AT] 发送失败: " + $_.Exception.Message) }
}
try{ $sp.Close() }catch{}
Emit '[AT] 串口已关闭'
"""


def _linux_to_win_path(p: str) -> str:
    r"""WSL 内的 Linux 路径 -> Windows 可访问路径 (供 interop 执行的 exe 读取)。

    /mnt/c/...  -> C:\...
    其余(ext4)  -> \\wsl.localhost\<发行版>\...
    """
    m = _WSL_MNT_RE.match(p)
    if m:
        rest = m.group(2) or ""
        return m.group(1).upper() + ":\\" + rest.replace("/", "\\")
    distro = os.environ.get("WSL_DISTRO_NAME") or "Ubuntu"
    return "\\\\wsl.localhost\\" + distro + p.replace("/", "\\")


def _win_to_wsl_path(p: str) -> str:
    r"""Windows 路径 -> WSL 内可用路径 (供 interop 执行 Windows exe 时定位本机文件)。

    C:\Users\...  -> /mnt/c/Users/...
    已是 POSIX 路径则原样返回。
    """
    p = p.strip().strip('"')
    m = re.match(r"^([A-Za-z]):[\\/](.*)$", p)
    if m:
        return "/mnt/" + m.group(1).lower() + "/" + m.group(2).replace("\\", "/")
    return p
