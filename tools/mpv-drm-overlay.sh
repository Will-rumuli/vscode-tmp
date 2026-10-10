#!/bin/bash
# DRM overlay video playback for Rockchip
# Stops gnome-shell, plays video via DRM overlay plane, then restarts gnome-shell
#
# Usage: mpv-drm-overlay.sh <video_file> [mpv_extra_args...]

set -e

VIDEO="${1:?Usage: $0 <video_file> [mpv_extra_args...]}"
shift
EXTRA_ARGS=("$@")

echo "=== DRM Overlay Video Playback ==="
echo "Video: $VIDEO"

# Step 1: Stop gnome-shell / gdm
echo "[1/4] Stopping gnome-shell..."
systemctl stop gdm 2>/dev/null || true
pkill -9 gnome-shell 2>/dev/null || true
pkill -9 Xwayland 2>/dev/null || true
pkill -9 mutter 2>/dev/null || true
sleep 2

# Step 2: Verify DRM is available
echo "[2/4] Checking DRM devices..."
ls -la /dev/dri/

# Step 3: Play video with DRM overlay
echo "[3/4] Playing video via DRM overlay..."
echo "      Press 'q' to quit mpv"

export LIBVA_DRIVER_NAME=rockchip
export LIBVA_DRIVERS_PATH=/usr/lib/aarch64-linux-gnu/dri

# Use --vo=drm for direct DRM output (bypass GPU compositing)
# --drm-drmprime-video-plane=overlay uses a dedicated overlay plane for video
# --drm-draw-plane=primary uses the primary plane for UI elements
# --hwdec=vaapi for hardware decode via MPP
mpv \
    --no-config \
    --vo=drm \
    --hwdec=vaapi \
    --drm-device=/dev/dri/card0 \
    --drm-drmprime-video-plane=overlay \
    --drm-draw-plane=primary \
    --fs \
    --no-osc \
    --no-osd-bar \
    "$VIDEO" \
    "${EXTRA_ARGS[@]}" \
    2>&1

MPV_EXIT=$?

# Step 4: Restart gnome-shell
echo "[4/4] Restarting gnome-shell..."
systemctl start gdm 2>/dev/null || true
sleep 3

echo "=== Done (exit: $MPV_EXIT) ==="
exit $MPV_EXIT
