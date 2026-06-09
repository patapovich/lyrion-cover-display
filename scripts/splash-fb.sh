#!/bin/sh
# Paint the boot splash to the firmware framebuffer (/dev/fb0) as early as
# possible and KEEP re-painting for a few seconds. The root fs is mounted by the
# initramfs before this runs, so splash.raw is available without waiting for
# local-fs.target. Re-painting defeats whatever clears the framebuffer during
# early boot and guarantees the splash is up the instant the slow HDMI scaler
# locks the signal (~4.5s). The app paints the cover later (same /dev/fb0).
RAW=__APPDIR__/assets/splash.raw
[ -r "$RAW" ] || exit 0

# Detach the framebuffer text console (belt-and-suspenders; fbcon=map:1 too).
for v in /sys/class/vtconsole/vtcon*; do
    case "$(cat "$v/name" 2>/dev/null)" in
        *[Ff]rame*buffer*) echo 0 > "$v/bind" 2>/dev/null || true ;;
    esac
done

# Paint the splash ONCE and leave it. The scaler doesn't start displaying the
# framebuffer until its cold-boot image-lock (~8-9s on this adapter, separate
# from the backlight); a single stable frame shows the instant it does. Repeated
# writes only tear, so we do not loop.
i=0
while [ "$i" -lt 100 ]; do
    if [ "$(cat /sys/class/graphics/fb0/virtual_size 2>/dev/null)" = "1600,1200" ]; then
        cat "$RAW" > /dev/fb0 2>/dev/null || true
        break
    fi
    i=$((i + 1))
    sleep 0.05
done
exit 0
