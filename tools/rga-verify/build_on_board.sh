#!/bin/sh
# 板上编译运行 RGA NV12 Transformer 验证程序（纯 C 版）
# 用法：sh build_on_board.sh
cd "$(dirname "$0")"

echo "== 1. 确认 /dev/rga 存在 =="
ls -l /dev/rga || { echo "FAIL: /dev/rga 不存在"; exit 1; }

echo "== 2. 确认 librga 存在 =="
ls /usr/lib/aarch64-linux-gnu/librga.so* 2>/dev/null || \
ls /usr/lib/*/librga.so* 2>/dev/null || { echo "FAIL: librga 不存在"; exit 1; }

echo "== 3. 确认头文件（若无 librga-dev，用预编译好的二进制即可）=="
if [ -f /usr/include/rga/im2d.h ] || [ -f /usr/include/rga/rga.h ]; then
    echo "头文件存在，尝试板上编译..."
    gcc -O2 -o rga_nv12_cvt rga_nv12_cvt.c -lrga || {
        echo "板上编译失败，改用预编译二进制"
        cp rga_nv12_cvt_arm64 rga_nv12_cvt
    }
else
    echo "无 librga-dev 头文件，直接使用预编译二进制 rga_nv12_cvt_arm64"
    cp rga_nv12_cvt_arm64 rga_nv12_cvt
fi

echo "== 4. 运行前 RGA 中断计数 =="
grep -i rga /proc/interrupts || true

echo "== 5. 运行验证 =="
./rga_nv12_cvt
RC=$?

echo "== 6. 运行后 RGA 中断计数（应增长）=="
grep -i rga /proc/interrupts || true

exit $RC
