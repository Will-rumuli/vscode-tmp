"""后端文案国际化。

设计: 以**中文原文为 key**。迁移时只需把面向用户的中文字面量包一层 L("中文..."),
译文表里查不到就原样返回中文 —— 所以未迁移的代码行为完全不变, 可以增量迁移。

语言来源优先级:
  1. set_lang() 显式设置 (由 server.py 中间件从 X-QPi-Lang 请求头读取)
  2. 环境变量 QPILANG
  3. 默认 "zh"
"""
from __future__ import annotations

import os

_DEFAULT = "zh"


def _from_env() -> str:
    return "en" if (os.environ.get("QPILANG") or "").lower().startswith("en") else _DEFAULT


_lang = _from_env()


def get_lang() -> str:
    """当前后端文案语言: "zh" | "en" """
    return _lang


def set_lang(lang: str | None) -> None:
    """设置后端文案语言; None/空串 = 回落到环境变量 QPILANG 决定的默认值"""
    global _lang
    if not lang:
        _lang = _from_env()
        return
    _lang = "en" if lang.lower().startswith("en") else "zh"


def L(zh: str, **vars: object) -> str:
    """取当前语言下 <zh> 对应的文案, 并替换 {name} 形式的占位符。

    zh 语言下直接返回原文; en 语言下查 MSG, 查不到回退原文。
    """
    s = MSG.get(zh, zh) if _lang == "en" else zh
    for k, v in vars.items():
        s = s.replace("{" + k + "}", str(v))
    return s


# ---- 译文表: 中文原文 -> 英文 ----
MSG: dict[str, str] = {
    "未在设备树源文件中找到节点 &{label}": "Node &{label} not found in devicetree source",
    "未找到 SDK 烧录脚本: {script}\n请确认已连接 {board} SDK (含 {rel})": "SDK flash script not found: {script}\nMake sure the {board} SDK is connected (containing {rel})",
    "无法在编译机定位 H1 烧录固件目录 (需同时含 prog_firehose_*.elf 与 partition_ufs/ 或 partition_emmc/)。\n请先在编译机完成「全量编译」生成固件; 若固件目录非默认位置, 可在 tools/VERSION.txt 增加声明: FLASH_FW_DIR=<相对 SDK 的固件目录>": "Cannot locate the H1 flash firmware directory on the build machine (must contain both prog_firehose_*.elf and partition_ufs/ or partition_emmc/).\nRun a full build on the build machine to generate firmware first; if the firmware directory is not in the default location, add a declaration to tools/VERSION.txt: FLASH_FW_DIR=<firmware dir relative to the SDK>",
    "固件目录不在 SDK 内, 无法拉取: {fw}": "Firmware directory is outside the SDK, cannot pull: {fw}",
    "echo '[QPi] 正在从编译机拉取 H1 烧录脚本与固件到本机 (固件较大, 请稍候)...'; {pull} && echo PULL_OK && cd {dir} && bash {script} ufs": "echo '[QPi] Pulling H1 flash scripts and firmware from the build machine (firmware is large, please wait)...'; {pull} && echo PULL_OK && cd {dir} && bash {script} ufs",
    "项目名仅限字母/数字/下划线/中划线 (1-64 字符)": "Project name may only contain letters/digits/underscores/hyphens (1-64 characters)",
    "模板不存在: {template_id}": "Template not found: {template_id}",
    "项目已存在: {name} (可先删除再创建)": "Project already exists: {name} (delete it first, then create again)",
    "非法项目名": "Invalid project name",
    "项目不存在: {name}": "Project not found: {name}",
    "本机烧录不需要 root": "Flashing on this machine does not require root",
    "sudo 免密可用": "Passwordless sudo available",
    "远端烧录 (qdl/upgrade_tool) 需要 sudo 权限": "Remote flashing (qdl/upgrade_tool) requires sudo privileges",
    "配置 Docker 需要 sudo 权限 (安装 docker / 构建镜像)": "Configuring Docker requires sudo privileges (installing docker / building images)",
    "编译在 Docker 容器内执行, 当前用户执行 docker 需要 sudo 密码": "Builds run inside a Docker container; the current user needs a sudo password to run docker",
    "Docker 可直接访问, 无需密码": "Docker is directly accessible, no password needed",
    "当前 SDK 不包含 docker_env/Dockerfile, 无法使用容器编译": "This SDK does not include docker_env/Dockerfile, so container builds are not available",
    "未检测到 Docker, 请先点击配置构建环境": "Docker not detected; click \"Configure Build Environment\" first",
    "清理残留编译容器": "Cleaning up leftover build container",
    "启动 Docker 编译容器": "Starting Docker build container",
    "该编译模式不需要 root": "This build mode does not require root",
    "该 SDK 的打包脚本不需要 root": "This SDK's packaging script does not require root",
    "探测失败, 跳过: {e}": "Probe failed, skipping: {e}",
    "打包 rootfs 需要 root 权限 (保真复制 base 属主)": "Packaging rootfs requires root privileges (to preserve base ownership)",
    "该 SDK 已有编译在运行 (进程: {pids}), 请等它结束或在任务列表里停止后再编译。\n并发编译会共用同一个 {dir} 目录, 导致 bitbake 互斥失效并死锁": "A build is already running for this SDK (processes: {pids}); wait for it to finish or stop it in the task list before building.\nConcurrent builds share the same {dir} directory, which breaks bitbake's mutex and causes a deadlock",
    "app 编译需要指定应用目录": "Building app requires specifying an application directory",
    "Yocto SDK 没有 app/overlays/rootfs 编译入口: 应用请用 BitBake (bitbake <recipe>), 根文件系统由 buildconfig + buildall 统一构建": "Yocto SDK has no app/overlays/rootfs build entry: use BitBake for applications (bitbake <recipe>); the root filesystem is built by buildconfig + buildall",
    "尚未保存构建配置, 无法执行{what}。请先点击侧边栏「配置构建环境」填写工程信息 (系统类型 / 项目 ID / 版本) 并保存, 之后编译会自动带上这些参数": "Build configuration not saved yet, cannot run {what}. Click \"Configure Build Environment\" in the sidebar, fill in the project info (system type / project ID / version) and save; builds will then include these parameters automatically",
    "bitbake $TARGET_IMAGE -c cleanall && rm -rf {build_dir} && echo '[QPi] 已删除构建目录 {build_dir}'": "bitbake $TARGET_IMAGE -c cleanall && rm -rf {build_dir} && echo '[QPi] Build directory deleted: {build_dir}'",
    "全量编译": "the full build",
    "编译 Boot": "the Boot build",
    "编译内核 (Boot + 内核)": "the kernel build (Boot + kernel)",
    "项目 ID 仅限字母/数字/下划线, 长度 1-64": "Project ID may only contain letters/digits/underscores, length 1-64",
    "系统类型必须是 {opts} 之一": "System type must be one of {opts}",
    "版本必须是 {opts} 之一": "Version must be one of {opts}",
    "Yocto 构建环境已就绪, 无需安装": "Yocto build environment is ready, nothing to install",
    "[QPi] 安装 Yocto 构建环境缺失依赖 ...": "[QPi] Installing missing Yocto build environment dependencies ...",
    "echo '{echo}'; {sudo} bash -c {inner} && echo '[QPi] 环境依赖安装完成'": "echo '{echo}'; {sudo} bash -c {inner} && echo '[QPi] Environment dependencies installed'",
    "部署目标必须是 boot/kernel/both 之一 (got {part})": "Deploy target must be one of boot/kernel/both (got {part})",
    "未找到编译产物 {fname} (quectel_build/output/ 下)。请先点击「编译内核」生成 {fname} 后再烧录": "Build artifact {fname} not found (under quectel_build/output/). Click \"Build kernel\" to generate {fname} before flashing",
    "无法自动定位设备上的 {disp} 分区 (候选标签: {labels})。请确认设备已连接 (adb devices), 或手动执行:\n  adb push {src} /tmp/qpi_{fname} && adb shell \"dd if=/tmp/qpi_{fname} of=<{disp}分区设备> bs=4M conv=fsync\"": "Cannot automatically locate the {disp} partition on the device (candidate labels: {labels}). Make sure the device is connected (adb devices), or run manually:\n  adb push {src} /tmp/qpi_{fname} && adb shell \"dd if=/tmp/qpi_{fname} of=<{disp} partition device> bs=4M conv=fsync\"",
    "echo '[QPi] 部署 {fname} -> {dev} ...' && {adb} push {src} /tmp/qpi_{fname} && {adb} shell 'dd if=/tmp/qpi_{fname} of={dev} bs=4M conv=fsync' && {adb} shell 'rm -f /tmp/qpi_{fname}'": "echo '[QPi] Deploying {fname} -> {dev} ...' && {adb} push {src} /tmp/qpi_{fname} && {adb} shell 'dd if=/tmp/qpi_{fname} of={dev} bs=4M conv=fsync' && {adb} shell 'rm -f /tmp/qpi_{fname}'",
    "内核 (Boot + 内核)": "Kernel (Boot + kernel)",
    "内核": "Kernel",
    " && {adb} shell 'sync' && echo '[QPi] {label} 部署完成, 请重启设备生效'": " && {adb} shell 'sync' && echo '[QPi] {label} deployment complete, reboot the device to take effect'",
    "qemu binfmt 已注册, 无需操作": "qemu binfmt already registered, no action needed",
    "echo '[QPi] 注册跨架构模拟: {names}'; {chain} && echo '[QPi] 完成, 应用层交叉编译现在可用'": "echo '[QPi] Registering cross-architecture emulation: {names}'; {chain} && echo '[QPi] Done, cross-compiling application code is now available'",
    "Yocto SDK 的构建环境配置 = buildconfig 流程: 请点击侧边栏「配置构建环境」, 按向导填写项目 ID, 选择系统类型 (LINUX/DEBIAN/UBUNTU) 与版本 (STD/DBG), 插件将自动执行 buildconfig": "Yocto SDK build environment setup = buildconfig flow: click \"Configure Build Environment\" in the sidebar and follow the wizard to fill in the project ID, system type (LINUX/DEBIAN/UBUNTU) and version (STD/DBG); the plugin will run buildconfig automatically",
    "镜像不存在: {img}\n请先完成「全量编译」生成 update.img": "Image not found: {img}\nRun a full build to generate update.img first",
    "远端镜像不存在: {img}\n请先完成「全量编译」生成 update.img": "Remote image not found: {img}\nRun a full build to generate update.img first",
    "烧录工具不可用: {tool}\n请确认 SDK 自带烧录工具 (tools/flash/, 由 tools/VERSION.txt 的 FLASH_TOOLS_* 声明) 或系统 PATH 中有该工具": "Flash tool unavailable: {tool}\nMake sure the SDK ships the flash tool (tools/flash/, declared by FLASH_TOOLS_* in tools/VERSION.txt) or that it is on PATH",
    "{exe} & echo '已启动 {name}, 请在图形界面中完成烧录'": "{exe} & echo 'Launched {name}, please complete flashing in the GUI'",
    "[提示] {exe} 为图形工具, 已自动改用命令行 {cli} 烧录": "[Hint] {exe} is a GUI tool; switched to the command-line {cli} for flashing",
    "{exe} wl 0 {img} && echo '(未找到 loader, 请确认设备处于 Maskrom 模式)'": "{exe} wl 0 {img} && echo '(No loader found, make sure the device is in Maskrom mode)'",
    "不支持的烧录工具: {tool}": "Unsupported flash tool: {tool}",
    "echo '[提示] 正在从 WSL 复制镜像到本机临时目录, 完成后自动烧录...'; {stage}{cmd}": "echo '[Hint] Copying image from WSL to a local temporary directory, flashing will start automatically...'; {stage}{cmd}",
    "远端编译机没有可用的烧录工具: {tool}\n请确认 SDK 已随包分发烧录工具 (tools/flash/, 由 tools/VERSION.txt 的 FLASH_TOOLS_* 声明), 或在编译机安装 (如 sudo apt install rkdeveloptool) 并加入 PATH": "No usable flash tool on the remote build machine: {tool}\nMake sure the SDK ships the flash tool (tools/flash/, declared by FLASH_TOOLS_* in tools/VERSION.txt), or install one on the build machine (e.g. sudo apt install rkdeveloptool) and add it to PATH",
    "{board} SDK 只支持整盘烧录, 请使用「全量烧录」": "{board} SDK only supports full-disk flashing, use \"Full flash\"",
    "请指定要烧录的分区名": "Please specify the partition name to flash",
    "图形烧录工具不支持单分区烧录, 请在图形界面中手动操作": "The GUI flash tool does not support single-partition flashing; operate manually in the GUI",
    "分区名不受支持: {partition}\nupgrade_tool 支持: {supported}": "Unsupported partition name: {partition}\nupgrade_tool supports: {supported}",
    "镜像不存在: {img}\n请先编译生成 {name}": "Image not found: {img}\nBuild {name} first",
    "未能从分区表找到分区: {partition}\n请确认设备已进入 Maskrom/Loader 模式, 或改用「全量烧录」": "Partition not found in the partition table: {partition}\nMake sure the device is in Maskrom/Loader mode, or use \"Full flash\"",
    "远端镜像不存在: {img}\n请先编译生成 {name}": "Remote image not found: {img}\nBuild {name} first",
    "远端编译机未找到 adb 命令。\n请在远端安装 platform-tools 并把 adb 加入 PATH, 例如:\n  sudo apt install android-tools-adb": "adb command not found on the remote build machine.\nInstall platform-tools on the remote machine and add adb to PATH, for example:\n  sudo apt install android-tools-adb",
    "WSL 下已尝试 Windows 侧 platform-tools (where adb) / 插件内置 / WSL PATH 中的 adb。\n板子 USB 接在 Windows 上, 请安装 Windows platform-tools (含 adb.exe 且在 Windows PATH 中) 后重试; 或安装 Linux adb 并用 usbipd-win 把 USB 直通进 WSL。": "Under WSL, tried Windows-side platform-tools (where adb) / bundled / adb in WSL PATH.\nThe board's USB is attached to Windows; install Windows platform-tools (with adb.exe in the Windows PATH) and retry, or install Linux adb and pass USB through with usbipd-win.",
    "macOS 下未找到 adb, 请安装 platform-tools:\n  brew install --cask android-platform-tools": "adb not found on macOS; install platform-tools:\n  brew install --cask android-platform-tools",
    "本机未找到 adb, 请安装 platform-tools:\n  sudo apt-get install -y android-tools-adb": "adb not found on this machine; install platform-tools:\n  sudo apt-get install -y android-tools-adb",
    "已找到 adb, 无需安装": "adb found, no installation needed",
    "远端设备: adb 需装在被连接的编译机上": "Remote device: adb must be installed on the connected build machine",
    "本机缺少 adb, 可自动安装": "adb is missing on this machine and can be installed automatically",
    "macOS 请手动安装: brew install --cask android-platform-tools": "On macOS please install manually: brew install --cask android-platform-tools",
    "WSL 下未找到 Windows 侧 adb, 请安装 Windows platform-tools": "No Windows-side adb found under WSL; install Windows platform-tools",
    "未找到 adb, 请安装 platform-tools": "adb not found; install platform-tools",
    "远端设备的 adb 请在编译机上安装, 插件不代为安装": "adb for a remote device must be installed on the build machine; the plugin does not install it for you",
    "当前宿主 ({host}) 不支持自动安装 adb, 请手动安装": "Automatic adb installation is not supported on the current host ({host}); please install manually",
    "本机已存在 adb, 无需安装": "adb already exists on this machine, no installation needed",
    "[QPi] 正在安装 adb ({pkg}) ...": "[QPi] Installing adb ({pkg}) ...",
    "Windows 下可使用插件内置 adb: tools/windows/adb/adb.exe\n或安装 platform-tools 并把 adb 加入 PATH": "Under Windows you can use the bundled adb: tools/windows/adb/adb.exe\nor install platform-tools and add adb to PATH",
    "未找到 adb 命令 (本机无可用 adb)\n": "adb command not found (no usable adb on this machine)\n",
    "请指定要推送的源文件/目录": "Please specify the source file/directory to push",
    "设备目标路径必须是以 / 开头的绝对路径": "The device target path must be an absolute path starting with /",
    "源不存在: {src}": "Source not found: {src}",
    "未检测到 adb 设备 (请确认设备已连接并授权)": "No adb device detected (make sure the device is connected and authorized)",
    "adb 设备未就绪: {serial} [{state}]": "adb device not ready: {serial} [{state}]",
    "采集设备信息失败: {e}": "Failed to collect device info: {e}",
    "Windows 串口 (WSL 转发)": "Windows serial port (WSL forwarding)",
    "远程串口 (编译机)": "Remote serial port (build machine)",
    "端侧设备 adb:{serial}": "On-device adb:{serial}",
    "不支持的校验位: {parity} (可选 N/E/O/M/S)": "Unsupported parity: {parity} (options N/E/O/M/S)",
    "不支持的流控: {flow} (可选 none/sw/hw)": "Unsupported flow control: {flow} (options none/sw/hw)",
    "不支持的停止位: {stopbits} (可选 1/1.5/2)": "Unsupported stop bits: {stopbits} (options 1/1.5/2)",
    "数据位 4 仅支持 adb 通道 (端侧设备串口); 本机/远端串口只支持 5-8": "Data bits 4 is only supported on the adb channel (on-device serial port); local/remote serial ports only support 5-8",
    "python3 -c 'import serial' 2>/dev/null || (echo '[AT] 远端缺少 pyserial, 请先在编译机执行: pip3 install pyserial' && exit 1)": "python3 -c 'import serial' 2>/dev/null || (echo '[AT] pyserial missing on the remote machine, run on the build machine first: pip3 install pyserial' && exit 1)",
    "非法的 Windows 串口名: {port}": "Invalid Windows serial port name: {port}",
    "非法的设备串口路径: {port}": "Invalid on-device serial port path: {port}",
    "if stty -F {port} {baud} raw -echo clocal {opt} 2>/dev/null; then echo {b64} | base64 -d > {port} 2>/dev/null; sleep 0.3; echo {b64} | base64 -d > {port} 2>/dev/null; timeout 2 cat {port} 2>/dev/null; else echo {q}无法配置串口 {port} (不存在或权限不足){q}; exit 1; fi": "if stty -F {port} {baud} raw -echo clocal {opt} 2>/dev/null; then echo {b64} | base64 -d > {port} 2>/dev/null; sleep 0.3; echo {b64} | base64 -d > {port} 2>/dev/null; timeout 2 cat {port} 2>/dev/null; else echo {q}Failed to configure serial port {port} (does not exist or permission denied){q}; exit 1; fi",
    "mkdir -p {dir} && {adb} shell {dev_capture} && {adb} pull /data/local/tmp/tinymix.txt {pull_dir}/ && {adb} pull /data/local/tmp/audio_dmesg.txt {pull_dir}/ && echo '音频日志已保存到 {dir}/'": "mkdir -p {dir} && {adb} shell {dev_capture} && {adb} pull /data/local/tmp/tinymix.txt {pull_dir}/ && {adb} pull /data/local/tmp/audio_dmesg.txt {pull_dir}/ && echo 'Audio logs saved to {dir}/'",
    "; [ -s {d}/tinymix.txt ] || echo '[QPi] 警告: tinymix.txt 为空 (设备既无 tinymix 也无 amixer, 或权限不足)'; [ -s {d}/audio_dmesg.txt ] || echo '[QPi] 警告: audio_dmesg.txt 为空 (dmesg 可能无权限或无匹配日志)'": "; [ -s {d}/tinymix.txt ] || echo '[QPi] Warning: tinymix.txt is empty (device has neither tinymix nor amixer, or permission denied)'; [ -s {d}/audio_dmesg.txt ] || echo '[QPi] Warning: audio_dmesg.txt is empty (dmesg may be restricted or have no matching logs)'",
    "if command -v tinymix >/dev/null 2>&1; then tinymix {raw}; elif command -v amixer >/dev/null 2>&1; then {amix}; else echo '[QPi] 设备无 tinymix/amixer' >&2; exit 1; fi": "if command -v tinymix >/dev/null 2>&1; then tinymix {raw}; elif command -v amixer >/dev/null 2>&1; then {amix}; else echo '[QPi] Device has no tinymix/amixer' >&2; exit 1; fi",
    "mkdir -p {out_dir} && {helper}{adb} shell {dev_script}; dest={out_dir}/{name}; destwin={pull_dir}/{name}; {adb} pull /data/local/tmp/qpi_shot.png \"$destwin\"; if [ -s \"$dest\" ]; then echo \"截图已保存: $dest\"; else rm -f \"$dest\"; echo '[QPi] 截图失败: 未生成有效图片 (需要已登录的 GNOME/Wayland 桌面会话; 设备侧原因见上方输出)' >&2; exit 1; fi": "mkdir -p {out_dir} && {helper}{adb} shell {dev_script}; dest={out_dir}/{name}; destwin={pull_dir}/{name}; {adb} pull /data/local/tmp/qpi_shot.png \"$destwin\"; if [ -s \"$dest\" ]; then echo \"Screenshot saved: $dest\"; else rm -f \"$dest\"; echo '[QPi] Screenshot failed: no valid image generated (requires a logged-in GNOME/Wayland desktop session; see the output above for device-side reasons)' >&2; exit 1; fi",
    "录制时长无效: {duration} (请填秒数, 如 10)": "Invalid recording duration: {duration} (enter seconds, e.g. 10)",
    "录制时长至少 1 秒": "Recording duration must be at least 1 second",
    "mkdir -p {out_dir} && {helper}{adb} shell {dev_script}; dev_out=$({adb} shell cat /data/local/tmp/qpi_rec_out.path 2>/dev/null | tr -d '\\r' | grep '^/' | head -n1); if [ -n \"$dev_out\" ]; then dest={out_dir}/qpi_record_$(date +%Y%m%d_%H%M%S).${dev_out##*.}; destwin={pull_dir}/$(basename \"$dest\"); {adb} pull \"$dev_out\" \"$destwin\"; if [ -s \"$dest\" ]; then echo \"录屏已保存: $dest\"; else rm -f \"$dest\"; echo '[QPi] 录屏失败: 录制文件为空' >&2; exit 1; fi; else echo '[QPi] 录屏失败: 未取到录制文件 (需要已登录的 GNOME/Wayland 桌面会话)' >&2; exit 1; fi": "mkdir -p {out_dir} && {helper}{adb} shell {dev_script}; dev_out=$({adb} shell cat /data/local/tmp/qpi_rec_out.path 2>/dev/null | tr -d '\\r' | grep '^/' | head -n1); if [ -n \"$dev_out\" ]; then dest={out_dir}/qpi_record_$(date +%Y%m%d_%H%M%S).${dev_out##*.}; destwin={pull_dir}/$(basename \"$dest\"); {adb} pull \"$dev_out\" \"$destwin\"; if [ -s \"$dest\" ]; then echo \"Recording saved: $dest\"; else rm -f \"$dest\"; echo '[QPi] Recording failed: recording file is empty' >&2; exit 1; fi; else echo '[QPi] Recording failed: no recording file obtained (requires a logged-in GNOME/Wayland desktop session)' >&2; exit 1; fi",
    "采集性能数据失败: {e}": "Failed to collect performance data: {e}",
    "(未找到 SKILL 文档: {name})": "(SKILL document not found: {name})",
    "未配置 SDK 目录": "SDK directory not configured",
    "SDK 目录不是 git 仓库": "SDK directory is not a git repository",
    "当前分支没有远端跟踪分支, 无法比较版本": "Current branch has no remote tracking branch, cannot compare versions",
    "找不到远端跟踪分支 {ref} (请先确认网络或手动 git fetch)": "Remote tracking branch {ref} not found (check the network or run git fetch manually)",
    "核心 SDK (build.sh + 工具链)": "Core SDK (build.sh + toolchain)",
    "内核源码 kernel/": "Kernel source kernel/",
    "构建工具 tools/": "Build tools tools/",
    "预编译资源 prebuilds/": "Prebuilt resources prebuilds/",
    "交叉工具链 toolchains/": "Cross toolchain toolchains/",
    "工程模板 docs/templates/": "Project templates docs/templates/",
    "SKILL 文档 skills/": "SKILL documents skills/",
    "设备树 Overlay overlay/": "Devicetree overlay overlay/",
    "组件 {rel} 未配置仓库地址": "Component {rel} has no repository URL configured",
    "{rel} 已存在, 如需覆盖请勾选强制": "{rel} already exists; check Force to overwrite",
    "未配置核心 SDK 仓库地址": "Core SDK repository URL not configured",
    "请指定克隆目标目录": "Please specify the clone target directory",
    "未指定保存目录": "Save directory not specified",
    "保存目录必须是绝对路径: {path}": "Save directory must be an absolute path: {path}",
    "目标目录已存在: {target}\n请删除后重试, 或换一个保存目录": "Target directory already exists: {target}\nDelete it and retry, or choose another save directory",
    "未配置下载源 (repo 或 archive 均为空)": "No download source configured (both repo and archive are empty)",
    "未找到板级设备树源文件 (请确认 SDK 内核含板级 dts/dtsi)": "Board-level devicetree source files not found (make sure the SDK kernel contains board dts/dtsi)",
    "未找到可编辑的板级源文件: 节点 &{node} (源 {source})": "Editable board-level source file not found: node &{node} (source {source})",
    "未在 {label} 中找到节点 &{node}": "Node &{node} not found in {label}",
    "清理": "cleanup",
    "该": "This",
    "未指定": "unspecified",
    # ---- models / executor / api (前端直连的错误与展示文案) ----
    "(未设置)": "(not set)",
    "SDK 目录不存在: {root}": "SDK directory not found: {root}",
    "远端 SDK 目录不存在: {root}": "Remote SDK directory not found: {root}",
    "WSL 发行版 {distro} 中 SDK 目录不存在: {root}\n请确认发行版名正确且 WSL 可启动 (wsl -d {distro} -- pwd)": "SDK directory not found in WSL distro {distro}: {root}\nMake sure the distro name is correct and WSL can start (wsl -d {distro} -- pwd)",
    "找不到 bash (git-bash)。请安装 Git for Windows, 或把 bash.exe 所在目录加入系统 PATH 后重启 VS Code": "bash (git-bash) not found. Install Git for Windows, or add the directory containing bash.exe to the system PATH and restart VS Code",
    "尚未配置连接, 请先 POST /api/connect": "Not connected yet; call POST /api/connect first",
    "sdk_root 不能为空": "sdk_root cannot be empty",
    "连接失败": "Connection failed",
}