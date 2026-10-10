/*
 * rga_nv12_cvt.c - 最小 RGA NV12 Transformer 验证程序（纯 C 版）
 *
 * 直接调用板上 librga 导出的 C 符号：
 *   wrapbuffer_virtualaddr_t()  +  imcvtcolor_t()
 * 做 YUYV(422) -> NV12(420SP) 颜色空间转换，验证 RGA 硬件加速可用。
 *
 * 用 mmap 分配页对齐内存，避免 handle 自动导入不一致问题。
 *
 * 交叉编译：
 *   aarch64-linux-gnu-gcc -O2 -o rga_nv12_cvt_arm64 rga_nv12_cvt.c \
 *     -I<librga-dev>/usr/include -I<librga-dev>/usr/include/rga \
 *     -L<librga-dev>/usr/lib/aarch64-linux-gnu \
 *     -L<libdrm>/usr/lib/aarch64-linux-gnu -lrga -ldrm
 *
 * 运行：./rga_nv12_cvt_arm64
 * 判定：打印 "imcvtcolor(YUYV->NV12) = SUCCESS" 即 RGA NV12 转换硬件可用。
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>

#include <rga/rga.h>   /* RK_FORMAT_* 枚举、rga_buffer_t */
#include <rga/im2d.h>  /* wrapbuffer_virtualaddr_t / imcvtcolor_t 声明 */

int main(void)
{
    const int w = 1920, h = 1080;
    const size_t src_size = (size_t)w * h * 2;      /* YUYV: 2 bytes/pixel */
    const size_t dst_size = (size_t)w * h * 3 / 2;  /* NV12: 1.5 bytes/pixel */

    /* mmap 分配页对齐内存（RGA 虚拟地址导入要求页对齐） */
    unsigned char *src = mmap(NULL, src_size, PROT_READ | PROT_WRITE,
                              MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    unsigned char *dst = mmap(NULL, dst_size, PROT_READ | PROT_WRITE,
                              MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (src == MAP_FAILED || dst == MAP_FAILED) {
        printf("mmap failed\n");
        return 1;
    }
    memset(src, 0x80, src_size);  /* 中灰 YUV */
    memset(dst, 0x00, dst_size);

    /* 两边都用底层 _t 函数包装虚拟地址（不显式 import handle） */
    rga_buffer_t s = wrapbuffer_virtualaddr_t(src, w, h, w, h, RK_FORMAT_YUYV_422);
    rga_buffer_t d = wrapbuffer_virtualaddr_t(dst, w, h, w, h, RK_FORMAT_YCbCr_420_SP);

    /* 核心：RGA 硬件做 YUYV -> NV12 颜色空间转换 */
    IM_STATUS ret = imcvtcolor_t(s, d, RK_FORMAT_YUYV_422, RK_FORMAT_YCbCr_420_SP,
                                 IM_COLOR_SPACE_DEFAULT, 1);

    if (ret == IM_STATUS_SUCCESS) {
        printf("imcvtcolor(YUYV->NV12) = SUCCESS  [RGA NV12 Transformer OK]\n");
    } else {
        printf("imcvtcolor(YUYV->NV12) = FAILED: %s\n", imStrError(ret));
    }

    /* 输出一小组结果值，便于确认数据真的被写过（非零） */
    printf("dst[0..7] = %02x %02x %02x %02x %02x %02x %02x %02x\n",
           dst[0], dst[1], dst[2], dst[3], dst[4], dst[5], dst[6], dst[7]);

    munmap(src, src_size);
    munmap(dst, dst_size);
    return ret == IM_STATUS_SUCCESS ? 0 : 3;
}
