# Quectel Pi

[English](https://github.com/Quectel-Pi/vscode-extension-quectelpi-issues/blob/main/document/quectel-pi/README.md) | **简体中文**

VS Code 插件，把 **Quectel Pi 系列** SDK 的整套流程搬进图形界面：下载 SDK → 连接 → 配置构建环境 → 编译 → 连接开发板 → 烧录 → 调试。

支持的 SDK：

| SDK | 芯片 | 构建体系 | 产物与烧录方式 |
| --- | --- | --- | --- |
| **M2 SDK** | RK3576 | 原生 `build.sh` | `update.img`，用烧录工具写入 |
| **H1 Simple SDK** | QCS6490 | 原生 `build.sh` | `efi.bin` + `dtb.bin` + `system.img`，EDL 全盘烧录 |
| **H1 Yocto SDK** | QCS6490 | Yocto / BitBake | firmware 目录，用 SDK 自带 `flash.sh` 烧录 |

---

# 第一部分 · 用户指南

> 这一部分写给使用者。所有操作都在 VS Code 里完成。

## 1. 开始之前

| 你需要准备 | 说明 |
| --- | --- |
| 一台电脑 | Linux（推荐 Ubuntu）或 Windows。**Windows 上 SDK 必须放在 WSL 里**，因为 SDK 的构建脚本是 Linux shell 脚本 |
| 开发板 | Quectel Pi M2（RK3576）或 H1（QCS6490），外加一根 USB 数据线 |
| 磁盘空间 | M2 / H1 Simple SDK 构建需要几 GB；**完整 Yocto 构建需要几十 GB** |
| VS Code | 版本 1.85 或更高 |
| SDK 源码 | 可以在插件里直接下载（见下方步骤 1），也可以使用你已有的 SDK 目录 |

可选：一台**远程 Linux 编译机**。如果你在服务器上构建，就在自己的电脑上装 VS Code 和本插件，通过 SSH 连接过去——见第 6 节。

**你不需要准备什么**：电脑上不用装 Python、Node.js 或任何命令行工具。插件首次启动时会自己把需要的运行环境准备好。

## 2. 安装插件

1. 打开 VS Code，点击活动栏的 **扩展** 图标（或按 `Ctrl+Shift+X`）。
2. 搜索 **Quectel Pi**，点击 **安装**。
   - 离线 / 内网环境：点击扩展视图顶部的 `...` → **从 VSIX 安装...**，选择你拿到的 `.vsix` 文件。
3. 活动栏会出现 **Quectel Pi** 图标——点它打开工具面板。

第一次打开面板时，插件会在后台准备自己的运行环境（为辅助服务创建一个私有的隔离环境）。只有首次启动需要等待片刻，之后再打开就是秒开。在连接 SDK 之前，面板头部的状态标签一直显示 **未连接**。

> **语言**：界面支持中文和英文。点击面板头部的 🌐 按钮即可切换。默认跟随你的 VS Code 显示语言，一旦手动切换过，就会记住你的选择。

## 3. 界面速览

所有功能分布在三个地方：

| 位置 | 有什么 |
| --- | --- |
| **侧边栏**（Quectel Pi 图标） | 主界面：可折叠的卡片分组——连接 / SDK、应用管理、编译、烧录 & 设备、日志 / 调试、多媒体、AI 助手 |
| **头部按钮** | 连接状态右侧的三个按钮：**刷新**（重新检查连接与 SDK 平台）、**重启后端**（辅助服务卡住时用）、**切换语言** |
| **状态栏**（窗口底部） | 五个一键入口：连接状态、编译应用、烧录 Boot、全量烧录、dmesg（实时内核日志） |

在卡片分组下方，同一个侧边栏还有 **管理资源** 视图——五个开工前要用的入口：**文档指南**（选择英文或中文官网）、**管理 SDK**、**打开已有应用**、**示例工程**（打开 Quectel-Pi 的 GitHub 组织页）、**打开终端**。

有些内容会以**独立标签页**的形式在编辑器区域打开：连接表单、AT 指令台、设备信息页、性能监控、SDK 信息页以及 Yocto 构建配置。每个标签页的名字和打开它的卡片一致。

有些命令没有对应的卡片，只能从**命令面板**（`Ctrl+Shift+P`，然后输入 `QPi SDK:`）调用：**tinymix 音频控制**、**环境自检**。

## 4. 走一遍完整流程：五步从编译到烧录

这是最短的完整路径，从一台空电脑到开发板跑起你自己编译的系统。

### 步骤 1 · 下载 SDK

在侧边栏找到 **管理资源**，点击 **管理 SDK**，然后选择 SDK 家族：

![选择 SDK 版本 —— M2 或 H1](https://raw.githubusercontent.com/Quectel-Pi/vscode-extension-quectelpi-issues/main/images/quectel-pi/step1-select-sdk.png)

*先选 SDK 。如果选 H1，会再问一次要 **Simple SDK** 还是 **Yocto SDK**；M2 只有一种。*

接着会问你保存到哪里：

![输入本地保存路径](https://raw.githubusercontent.com/Quectel-Pi/vscode-extension-quectelpi-issues/main/images/quectel-pi/step1-save-path.png)

*输入一个绝对路径后按 `Enter`——SDK 会在一个专用终端里 `git clone` 到该目录，进度实时可见。关闭那个终端即中止下载。*

- Linux / WSL 下可以用类似 `/home/<用户名>/qpi-sdk` 的路径。
- Windows 下路径必须在 WSL 内（例如 `/home/<用户名>/qpi-sdk`），见第 6 节。
- 下载视网络情况可能需要一段时间。

### 步骤 2 · 连接 SDK

点击 **连接 / SDK** 分组里的 **连接 SDK**，编辑器区域会打开一个表单：

![连接 QPi SDK 表单](https://raw.githubusercontent.com/Quectel-Pi/vscode-extension-quectelpi-issues/main/images/quectel-pi/step2-connect-sdk.png)

*模式保持 **本机**，填入 SDK 根目录（就是你刚克隆下来的那个文件夹）。如果 SDK 在远程编译机上，改选 **SSH 远程** 并填写主机 / 用户 / 密码。点击 **连接**。*

连接成功后，面板头部的状态标签会从 **未连接** 变为 SDK 类型和路径（例如 `Local @ /home/will/qpi/h1`），**编译** 与 **烧录 & 设备** 分组里的按钮也会自动适配识别到的 SDK。

连接配置会被记住，下次启动 VS Code 时插件会自动重连。

### 步骤 3 · 配置构建环境

点击 **编译** 分组里的 **配置构建环境...**：

![配置构建环境，以及它的终端日志](https://raw.githubusercontent.com/Quectel-Pi/vscode-extension-quectelpi-issues/main/images/quectel-pi/step3-setup-env.png)

*这一步会运行 SDK 自带的环境脚本：检查并安装构建所需的软件包，并下载预编译的底包镜像。安装进度会实时输出到终端。*

- 如果缺少系统软件包，会要你输入一次登录密码（就是存放 SDK 那台机器的常规 `sudo` 密码）。
- 每套 SDK 跑一次即可；但如果构建时报缺工具，就再跑一次。

### 步骤 4 · 编译

点击 **编译** 分组里的 **全量编译**：

![正在编译，日志在终端里滚动](https://raw.githubusercontent.com/Quectel-Pi/vscode-extension-quectelpi-issues/main/images/quectel-pi/step4-build-all.png)

*编译在独立终端里进行，完整日志实时输出。Yocto SDK 的首次构建耗时很长，之后是增量构建。*

- 也可以只编译 bootloader（**编译 Boot**）；在 Yocto SDK 上则可以把 bootloader 和内核一起编译（**编译内核**）。
- 想从零开始构建时，用 **清理产物** 删除中间产物。
- 关闭终端即中止编译——想停掉一次漫长的构建时很好用。

### 步骤 5 · 连接开发板并烧录

1. 用 USB 线把开发板连到电脑。
2. 点击 **设备信息**，确认开发板已被识别（型号、OS 版本、内存、存储）。
3. 让开发板进入下载模式：
   - M2：点击 **进 Loader**
   - H1：点击 **进 EDL**（高通 9008 下载模式）
4. 点击 **全量烧录**，等待完成。M2 会让你选烧录工具——插件自带所需工具，Windows 上不需要另外安装任何东西。
5. 点击 **重启设备**。开发板就会启动到你刚编译好的系统。

## 5. 三套 SDK 详解

### 5.1 M2 SDK（RK3576）

1. **下载**：管理资源 → 管理 SDK → **M2** → 绝对路径 → `Enter`。
2. **连接**：连接 SDK，模式选 **本机**（或在服务器上构建时选 **SSH 远程**）。
3. **配置构建环境...**：安装构建依赖和预编译镜像。
4. **全量编译**：产出 `update.img`。**编译 Boot** 只编译 bootloader。
5. **设备信息**：用 USB 插上开发板，确认已被识别。
6. **进 Loader**：让开发板进入 Rockchip loader 模式。
7. **全量烧录**：选择烧录工具（`upgrade_tool` / `rkdeveloptool` / `RKDevTool`）并写入 `update.img`；也可以单独用 **烧录 Boot** 写 boot 分区。
8. **重启设备**。

### 5.2 H1 Simple SDK（QCS6490）

1. **下载**：管理资源 → 管理 SDK → **H1** → **H1 Simple SDK** → 绝对路径 → `Enter`。
2. **连接**：同上。
3. **配置构建环境...**：安装依赖并下载预编译底包镜像。
4. **全量编译**：产出各分区镜像；**编译 Boot** 只编译 `efi.bin` / `dtb.bin`。
5. **设备信息**：用 USB 插上开发板，确认已被识别。
6. **进 EDL**：让开发板进入高通 EDL 9008 下载模式。
7. **全量烧录**：运行 SDK 自带的 `flash.sh` 写整盘。高通平台不支持单分区烧录。
8. **重启设备**。

### 5.3 H1 Yocto SDK（QCS6490）

1. **下载**：管理资源 → 管理 SDK → **H1** → **H1 Yocto SDK** → 绝对路径 → `Enter`。
2. **连接**：同上。
3. **配置构建环境...**：你需要填写项目 ID、操作系统（`LINUX` / `UBUNTU` / `DEBIAN`）和版本（`STD` 发布 / `DBG` 调试），还可勾选安全启动；面板会保存这些参数，之后每次构建自动套用。同一步骤会在你输入 `sudo` 密码后安装缺失的系统依赖。
4. **编译**：
   - **全量编译**：产出用于烧录的 firmware 目录。
   - **编译内核**：产出 `dtb.bin`（bootloader）和 `efi.bin`（内核）。比全量构建快得多。
5. **设备信息**：用 USB 插上开发板，确认已被识别。
6. **烧录**：
   - **全量烧录**：先进入 EDL，然后把固件写入整盘。
   - **烧录内核**：不需要 EDL——内核产物直接写入 DTB 和 ESP 分区。如果产物还不存在，会提示你先编译。
7. **重启设备**。

## 6. 连接方式

| 你的电脑 | 模式 | SDK 在哪里 |
| --- | --- | --- |
| **Linux** | 本机 | 这台 Linux 机器上的某个目录——最常见的情况 |
| **Linux** | SSH 远程 | 一台远程 Ubuntu / Linux 编译机 |
| **Windows** | 本机 | **在 WSL 内**（例如 `/home/<用户>/qpi-sdk`）——构建脚本需要 Linux |
| **Windows** | SSH 远程 | 一台远程 Ubuntu / Linux 编译机；你的电脑只作为界面 |

注意事项：

- **Windows 上**，插件本身可以运行在 Windows，但 SDK 必须放在 WSL 内。请用 WSL 路径连接，不要用 `C:\...` 路径。
- **SSH 模式下**，如果开发板插在**你自己的电脑**上、而编译产物在**远程机器**上，插件会先把产物复制到本地临时目录，再用本机的烧录工具写入开发板。你也可以把开发板直接插在远程机器上。
- **远程机器上需要有自己的 VS Code Server**——如果 SSH 会话建立失败，插件会显示连接日志。

## 7. 功能详解

按分组逐条说明每个卡片能做什么。

### 连接 / SDK

- **连接 SDK** —— 连接一个 SDK（本机或 SSH）。表单见步骤 2。
- **断开** —— 断开当前连接，不会删除任何东西。
- **刷新设备** —— 重新扫描通过 USB 连接的开发板。
- **SDK 信息** —— 打开一个标签页，显示当前分支、当前提交、你的分支是否落后于远端，以及最近的提交记录。
- **后端信息** —— 打开一个标签页，显示插件辅助服务的地址与状态。看起来不对劲时很有用。
- **环境引导** —— 打开环境引导：检查 SDK 目录下的 8 个组件（核心 SDK、内核源码、构建工具、预编译资源、交叉工具链、工程模板、SKILL 文档、设备树 Overlay），告诉你缺哪些，并支持从仓库克隆补齐。

### 应用管理

- **创建应用** —— 向导式：选模板 → 填写模板变量（有下拉的必须从下拉里选）→ 给工程命名。字母、数字、下划线、连字符，1–64 个字符。工程创建在 SDK 的 `projects/` 目录下。
  - 模板来自 SDK 本身（`docs/templates/`）。如果你的 SDK 没有附带模板，向导会明确提示并终止。
- **应用列表** —— 列出 `projects/` 下的每个工程，各自带 *打开目录 / 编译 / 删除*（删除会要求确认）。
- **SKILL 文档** —— SDK `skills/` 目录下随 SDK 附带的文档，在 VS Code 标签页中打开。

### 编译

- **配置构建环境...** —— 步骤 3 描述的一次性（或更新后的）环境准备。
- **全量编译** —— 完整构建，产出可用于烧录的镜像。
- **编译 Boot** / **编译内核** —— 两者只会出现一个，取决于 SDK。Simple SDK 上编译 bootloader；Yocto SDK 上把 bootloader 和内核一起编译。
- **清理产物** —— 删除构建中间产物，让下次构建从零开始。

> **编译应用**（状态栏里的 *编译应用* 入口）会列出 SDK `projects/` 目录下已有的工程并编译你所选的那个。如果还没有工程，会提示你先用 **创建应用** 建一个。Yocto SDK 不参与应用编译：应用请改用 BitBake 构建。

### 烧录 & 设备

- **全量烧录** —— 写整盘。M2 会让你选工具（插件自带）；H1 运行 SDK 的 `flash.sh`。
- **烧录 Boot** / **烧录内核** —— 与 *编译 Boot / 编译内核* 对应。高通平台上无法烧写单个分区，所以 *烧录 Boot* 会提示你改用 **全量烧录**；在 Yocto SDK 上 *烧录内核* 会把内核产物写入 DTB 和 ESP 分区。
- **进 Loader / 进 EDL** —— 让开发板进入下载模式（M2 → Loader，H1 → EDL 9008）。
- **部署文件** —— 把电脑上的一个文件复制到开发板的某个目录（默认 `/userdata/<文件名>`）。
- **设备信息** —— 型号、OS 版本、内存、存储、网络等等。
- **重启设备** —— 重启开发板。

### 日志 / 调试

- **adb 命令** —— 一个已经连上开发板的终端，可以直接敲 `adb shell` 命令。
- **dmesg** —— 实时内核日志。
- **journalctl** —— 实时系统日志。
- **AT 指令** —— 串口控制台：选择端口和波特率，与模块收发 AT 指令。可以走开发板自己的串口、电脑的串口，或远程编译机的串口。
- **Shell** —— 在连上开发板的终端里执行 shell 命令。
- **抓取音频日志** —— 收集音频日志与配置，用于排查问题。

### 多媒体

- **音频播放** / **视频播放** —— 播放开发板上的文件。
- **录屏** / **截图** —— 抓取开发板屏幕。开发板需要接了显示器并已登录桌面会话。
- **性能监控** —— 一个实时曲线标签页，通过 adb 展示帧率、CPU 占用与频率、GPU 占用与频率以及温度。采样间隔可调，随时可暂停。开发板未暴露的指标显示为「无数据」。

### AI 助手

- **AI 助手** —— 打开 GitHub Copilot Chat（未安装时会询问是否安装）。
- **API 密钥设置** —— 打开用于接入第三方 AI 服务（Base URL / API Key）的设置页。

## 8. 常见问题

**面板一直显示「未连接」。**
点击 **连接 SDK** 并填入 SDK 目录。首次连接成功后插件会记住配置并自动重连。

**它向我要密码。**
安装构建依赖和烧录都需要存放 SDK 那台机器的管理员权限。请输入你的登录密码。如果配置了免密 `sudo`，直接按 `Enter` 即可。

**我关了终端，任务就停了。**
这是有意设计的：每个任务在自己独立的终端里运行，关闭那个终端即中止任务。这也是停掉一次漫长构建最快的方式。

**提示「未找到可用的烧录工具」。**
Windows 上插件自带所需工具。Linux 上请确认你的 SDK 提供了这些工具，或自行安装（`upgrade_tool`、`rkdeveloptool`）——插件会告诉你它找过哪些。

**怎么在中英文之间切换？**
面板头部的 🌐 按钮。侧边栏、状态栏以及所有已打开的标签页会立即切换。

**怎么改插件本身的设置？**
在 VS Code 设置里搜索 `qpiSdk`：

| 设置项 | 默认值 | 说明 |
| --- | --- | --- |
| `qpiSdk.backend.python` | 空（自动探测） | 辅助服务使用的 Python 解释器路径 |
| `qpiSdk.backend.autoStart` | true | 随 VS Code 一起启动辅助服务 |
| `qpiSdk.backend.mode` | local | `local` = 辅助服务在本机；`remote` = 使用远端机器上的辅助服务 |
| `qpiSdk.backend.remoteUrl` | http://127.0.0.1:8765 | `remote` 模式下辅助服务的地址 |
| `qpiSdk.backend.remoteKey` | 空 | `remote` 模式下辅助服务的密钥 |
| `qpiSdk.backend.pythonMirror` | 空（按网络自动选择） | 下载内置 Python 运行时所用的镜像 |
| `qpiSdk.backend.pipMirror` | 空（按网络自动选择） | 安装辅助服务依赖所用的包索引 |

**本文档里的截图是什么环境下的？**
是在 Linux（WSL）环境 + H1 Simple SDK 下截取的。其他平台上布局完全一致，只有 SDK 相关的按钮不同，具体见第 5 节。

## 9. 问题反馈

遇到问题、有疑问或者想提建议？欢迎提交 issue：

**https://github.com/Quectel-Pi/vscode-extension-quectelpi-issues/issues**

反馈时建议附上：你的操作系统（以及 SDK 是否在 WSL 内）、SDK 类型（M2 / H1 Simple / H1 Yocto）、你做了什么、期望的结果以及实际现象。最有用的材料是执行任务那个终端里的日志。

---

# 第二部分 · 开发与维护

> 这一部分写给插件的开发者和维护者。使用插件不需要看这一部分。

## 1. 架构

```
侧边栏 / 状态栏 / webview 标签页 ──postMessage──▶ extension.ts ──HTTP + SSE──▶ FastAPI (python)
                界面                                │                              │
                                            连接配置                         executor (local / SSH)
                                          (workspaceState)                        │
                                                              build.sh / bitbake / adb / qdl
```

- **前端**：TypeScript 插件 + webview（纯 HTML/CSS/JS，不依赖框架，自适应亮色与暗色主题）
- **后端**：Python FastAPI，监听随机 `127.0.0.1` 端口；每个请求都必须携带 `X-API-Key`；虚拟环境及其依赖在首次启动时创建
- **执行器**：本机（子进程）或通过 SSH 连接到远程 Linux 编译机（paramiko）
- **任务模型**：每个编译 / 烧录 / 日志任务都会打开一个专用的 VS Code 终端并通过 SSE 输出流；关闭终端会杀掉后端任务

## 2. 目录结构

```
src/                     插件前端（TypeScript）
  extension.ts           入口：注册命令，创建侧边栏 / 状态栏 / 标签页
  sidebar.ts             侧边栏卡片（分组 + 磁贴定义）
  welcome.ts             "管理资源" TreeView
  actions.ts             所有命令的实现
  api.ts                 HTTP 客户端（对应后端 /api/*）
  backend.ts             后端进程管理（spawn / 端口 / 密钥 / venv）
  statusbar.ts           状态栏项
  i18n.ts                前端文案（中/英）
  *Panel.ts              webview 标签页（连接 / AT / 设备树 / 设备信息 / 性能 / SDK 信息）
  webview/               webview 静态资源（HTML/CSS/JS）
python/
  server.py              FastAPI 入口（打印 READY port=<port> key=<key>）
  quecpi/                实现：api.py 路由 / sdk_ops.py SDK 操作 / i18n.py 后端文案
  requirements.txt       后端依赖
tools/                   随包分发的二进制文件，例如烧录工具
images/                  本文档使用的截图
```

## 3. 开发环境

| 依赖 | 版本 | 说明 |
| --- | --- | --- |
| Node.js | 编译需 ≥ 18，打包需 ≥ 20 | TypeScript 编译与 vsce |
| Python | 3.9+（推荐 3.12） | FastAPI 后端；venv 首次启动时自动创建 |
| Git | 任意 | 后端会执行本地 git 命令 |

```bash
npm install                                   # 1. 安装 TS 依赖
npm run compile                               # 2. 编译 TypeScript（输出到 out/）
python3 -m venv python/.venv                  # 3. 创建 Python venv（可选，会自动创建）
python/.venv/bin/python -m pip install -r python/requirements.txt
python/.venv/bin/python python/server.py      # 4.（可选）单独运行后端以便调试
```

成功后后端会打印 `READY port=<port> key=<key>`；插件用这一行来与它通信。

## 4. 构建 / 打包 / 安装

```bash
npm run package        # 推荐：封装了下面的 vsce 命令
# 或者
npx vsce package --no-dependencies --allow-missing-repository --skip-license --no-rewrite-relative-links
```

有两个打包细节值得了解：

- 需要 `--allow-missing-repository`，因为 `package.json` 里没有 `repository` 字段。
- `--no-rewrite-relative-links` 会保持本文档中的相对链接（`README.zh-CN.md`）不变。截图已改为托管在 `Quectel-Pi/vscode-extension-quectelpi-issues` 仓库中的绝对 URL，因此 vsce 不再需要仓库地址来解析图片路径。

产物是 `quectelpi-<version>.vsix`，通过 VS Code 的 **从 VSIX 安装...** 安装。

一键构建并安装（改写 `package.json` 里的 `version`、编译、打包、卸载旧版本并安装新版本）：

```bash
build-install.bat <version>       # Windows
./build-install.sh <version>      # Linux / macOS
```

版本号参数可选（不填会提示输入），接受 `vX.Y.Z` 或 `X.Y.Z`。

## 5. 调试

在 VS Code 中按 **F5** 启动扩展开发宿主。它会启动后端并打开侧边栏；`src/*.ts` 和 `python/quecpi/*.py` 里都能正常命中断点。

## 6. 两个约定

**本地化。** 前端文案在 `src/i18n.ts`，后端文案在 `python/quecpi/i18n.py`。切换语言会重建侧边栏、状态栏以及所有已打开的标签页；请求会携带 `X-QPi-Lang` 头，后端以对应语言应答。注意 `package.json` 里的 `%key%` 标题在扩展加载时只解析一次，所以运行时切换语言必须在代码里重置它们（例如 `view.title`）。

**打包注意事项。** `tools/fix_vsix.py` 和 `tools/sshrun.py` 是加密后的二进制（不是明文 Python），无法执行，也没有被任何脚本引用。`npm run package` 曾经会调用 `fix_vsix.py`，但它会以 `SyntaxError: source code cannot contain null bytes` 失败；这一步已经移除。包内不再分发烧录工具：Rockchip 的宿主机烧录工具由 SDK 随包分发在 `tools/flash/`，通过 SDK 的 `tools/VERSION.txt` 里的 `FLASH_TOOLS_*` 声明，扩展直接就地引用。包内只保留 Windows 版 `adb`（exe + 2 个 DLL），当 Windows 的 `PATH` 里没有 `adb` 时暂存到 `%LOCALAPPDATA%\QuectelPi\adb\`。