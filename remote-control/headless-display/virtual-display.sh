#!/bin/sh
# Give GNOME Remote Desktop a monitor on a workstation with no screen attached.
# It forces a disconnected integrated-GPU DisplayPort connector "on" through
# DRM debugfs (needs passwordless `sudo -n` for exactly the two commands
# below), adds a 1080p mode, and places it next to any real monitor.
# The defaults match an AMD iGPU (vendor 0x1002); set the variables for yours.
set -eu

CONNECTOR="${VD_CONNECTOR:-DP-4}"            # name under /sys/class/drm/cardN-*
XRANDR_OUTPUT="${VD_XRANDR_OUTPUT:-DisplayPort-3}"
PRIMARY_OUTPUT="${VD_PRIMARY_OUTPUT:-HDMI-A-1}"
GPU_VENDOR="${VD_GPU_VENDOR:-0x1002}"

export DISPLAY="${DISPLAY:-:0}"
export XAUTHORITY="${XAUTHORITY:-/run/user/$(id -u)/gdm/Xauthority}"

force_path=
for connector in /sys/class/drm/card*-"$CONNECTOR"; do
    [ -e "$connector" ] || continue
    card_name=${connector##*/}
    card_name=${card_name%-"$CONNECTOR"}
    gpu_path=$(readlink -f "/sys/class/drm/$card_name/device")
    [ -n "$gpu_path" ] || continue
    [ "$(cat "$gpu_path/vendor")" = "$GPU_VENDOR" ] || continue
    force_path="/sys/kernel/debug/dri/${gpu_path##*/}/$CONNECTOR/force"
    break
done

[ -n "$force_path" ] || exit 1
sudo -n test -e "$force_path"
printf on | sudo -n tee "$force_path" >/dev/null

connected=false
for _ in 1 2 3 4 5; do
    if timeout 30 xrandr --query | grep -q "^$XRANDR_OUTPUT connected"; then
        connected=true
        break
    fi
    sleep 2
done
[ "$connected" = true ] || exit 1

xrandr --newmode 1920x1080_60.00 173.00 1920 2048 2248 2576 1080 1083 1088 1120 -hsync +vsync 2>/dev/null || true
xrandr --addmode "$XRANDR_OUTPUT" 1920x1080_60.00 2>/dev/null || true

if timeout 30 xrandr --query | grep -Eq "^$PRIMARY_OUTPUT connected (primary )?[0-9]+x[0-9]+"; then
    xrandr --output "$XRANDR_OUTPUT" --mode 1920x1080_60.00 --right-of "$PRIMARY_OUTPUT"
    xrandr --output "$PRIMARY_OUTPUT" --primary
else
    xrandr --output "$XRANDR_OUTPUT" --mode 1920x1080_60.00 --primary
fi

logger -t virtual-display "1920x1080 virtual output active on $XRANDR_OUTPUT"
