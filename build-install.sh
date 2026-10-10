#!/usr/bin/env bash
# ============================================================
# QPi SDK VS Code 扩展 一键构建安装脚本
#
# 用法:
#   ./build-install.sh            # 交互式输入版本号 (如 v0.3.8)
#   ./build-install.sh v0.3.8     # 直接带版本号
#
# 流程: 改 package.json 版本 -> tsc 编译 -> vsce 打包
#       -> 卸载旧版扩展 -> 安装新 vsix
# ============================================================
set -euo pipefail

cd "$(dirname "$0")"

EXT_ID="Quectel.quectelpi"
# 扩展安装目录: 桌面版 VS Code 在 ~/.vscode/extensions
# Remote-SSH / WSL / Dev Container 场景在 ~/.vscode-server/extensions
EXT_DIR=""
for d in "$HOME/.vscode/extensions" "$HOME/.vscode-server/extensions" "$HOME/.vscode-insiders/extensions"; do
    [[ -d "$d" ]] || continue
    if ls -d "$d"/${EXT_ID}-[0-9]* >/dev/null 2>&1; then
        EXT_DIR="$d"; break
    fi
    [[ -z "$EXT_DIR" ]] && EXT_DIR="$d"
done
[[ -n "$EXT_DIR" ]] || EXT_DIR="$HOME/.vscode/extensions"
echo "==> 扩展目录: $EXT_DIR"

# ---------- 1. 获取版本号 ----------
VERSION="${1:-}"
if [[ -z "$VERSION" ]]; then
    read -rp "请输入版本号 (如 v0.3.8): " VERSION
fi
VER="${VERSION#v}"                                  # 去掉开头的 v
if [[ ! "$VER" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "错误: 版本号格式不对 -> '$VERSION' (示例: v0.3.8 或 0.3.8)"
    exit 1
fi
echo "==> 目标版本: $VER"

# ---------- 2. 检查工具 ----------
command -v code >/dev/null 2>&1 || { echo "错误: 未找到 code CLI,请确认 VS Code 已安装且 code 在 PATH 中"; exit 1; }
command -v npm  >/dev/null 2>&1 || { echo "错误: 未找到 npm"; exit 1; }
[[ -d node_modules ]] || { echo "错误: 缺少 node_modules,请先执行 npm install"; exit 1; }

# ---------- 3. 更新 package.json 版本 ----------
echo "==> 更新 package.json 版本为 $VER"
node -e "
const fs = require('fs');
const p = 'package.json';
const j = JSON.parse(fs.readFileSync(p, 'utf8'));
j.version = '$VER';
fs.writeFileSync(p, JSON.stringify(j, null, 2) + '\n');
console.log('    package.json version -> ' + j.version);
"

# ---------- 4. 编译 ----------
echo "==> 编译 (npm run compile)"
npm run compile

# ---------- 5. 打包 ----------
echo "==> 打包 VSIX"
npm run package
VSIX="quectelpi-$VER.vsix"
if [[ ! -f "$VSIX" ]]; then
    echo "错误: 打包后未找到 $VSIX"
    exit 1
fi

# ---------- 6. 卸载旧版本 ----------
echo "==> 卸载旧版本扩展"
# VS Code 按扩展 ID 卸载,一次即可清掉注册信息;再手动删除磁盘上的旧版本目录
code --uninstall-extension "$EXT_ID" >/dev/null 2>&1 && echo "    已卸载 $EXT_ID (注册信息)" || echo "    (扩展未安装或已卸载,继续)"
OLD_DIRS=$(ls -d "$EXT_DIR"/${EXT_ID}-[0-9]* 2>/dev/null | grep -v -- "-$VER" || true)
if [[ -n "$OLD_DIRS" ]]; then
    for d in $OLD_DIRS; do
        rm -rf "$d" 2>/dev/null && echo "    已删除: $(basename "$d")" || echo "    删除失败: $(basename "$d") (文件被占用,VS Code 开着时旧后端进程会锁住 python/ 目录)"
    done
fi
REMAIN=$(ls -d "$EXT_DIR"/${EXT_ID}-* 2>/dev/null | grep -v -- "-$VER" || true)
if [[ -n "$REMAIN" ]]; then
    echo "    警告: 以下旧版本目录残留:"
    for d in $REMAIN; do echo "      $d"; done
    echo "    请重启 VS Code 后重跑本脚本,或手动删除:"
    echo "      rm -rf $EXT_DIR/${EXT_ID}-[0-9]*"
fi

# ---------- 7. 安装新版本 ----------
echo "==> 安装 $VSIX"
code --install-extension "$VSIX" --force

echo ""
echo "完成! 已安装 $EXT_ID @ $VER"
echo "请重启 VS Code (或执行命令: QPi SDK: 重启后端服务)"
