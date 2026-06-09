#!/usr/bin/env bash
# maintenance — toggle the read-only overlay filesystem on the Lyrion Cover
# Display kiosk, so the SD card can survive unclean power-offs in normal use
# but still be edited when you need to.
#
# Installed as /usr/local/sbin/maintenance. Usage:
#   sudo maintenance status   # show overlay / boot-partition read-only state
#   sudo maintenance rw        # disable overlay + reboot -> root is WRITABLE
#   sudo maintenance ro        # enable overlay  + reboot -> root is READ-ONLY
#
# Typical edit cycle:
#   sudo maintenance rw        # reboot to writable
#   ...edit the app's config.ini, deploy, etc...
#   sudo maintenance ro        # reboot back to the locked, power-safe state
#
# The boot partition (/boot/firmware) is left writable in both states so
# cmdline.txt / config.txt stay editable for recovery.
set -euo pipefail

need_root() {
    if [ "$(id -u)" -ne 0 ]; then
        echo "Run with sudo: sudo maintenance ${1:-}" >&2
        exit 1
    fi
}

on_off() { [ "$1" = "0" ] && echo ON || echo OFF; }   # raspi-config: 0=on, 1=off

status() {
    need_root status
    echo "overlay (root read-only): $(on_off "$(raspi-config nonint get_overlay_now)")"
    echo "boot partition read-only: $(on_off "$(raspi-config nonint get_bootro_now)")"
}

case "${1:-status}" in
    status)
        status
        ;;
    rw)
        need_root rw
        raspi-config nonint disable_overlayfs
        echo "Overlay OFF. Rebooting to a WRITABLE card in 3s..."
        echo "(run 'sudo maintenance ro' to lock it again when done)"
        sleep 3
        systemctl reboot
        ;;
    ro)
        need_root ro
        raspi-config nonint enable_overlayfs
        echo "Overlay ON. Rebooting to a READ-ONLY (power-safe) card in 3s..."
        sleep 3
        systemctl reboot
        ;;
    *)
        echo "usage: sudo maintenance {status|rw|ro}" >&2
        exit 2
        ;;
esac
