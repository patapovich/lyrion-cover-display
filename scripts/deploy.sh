#!/usr/bin/env bash
# deploy — push updated app code / splash assets into the running kiosk after a
# `git pull` (or after regenerating the splash with tools/gen_splash.py).
#
# The boot splash lives in TWO places: the live asset (assets/splash.raw, read
# at boot by splash-fb.service and live by the app) and a COPY baked into the
# initramfs by the coversplash hook at `update-initramfs` time. Regenerating
# splash.raw without rebuilding the initramfs leaves the early (initramfs) splash
# stale — its text sits at the old position while later screens use the new one.
# This script re-embeds the current assets and restarts the display.
#
# Run on the Pi (root needed for update-initramfs + systemctl):
#   sudo ./scripts/deploy.sh
set -euo pipefail

APPDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ "$(id -u)" -ne 0 ]; then
  echo "Please run with sudo: sudo ./scripts/deploy.sh" >&2
  exit 1
fi

# The root fs is mounted read-only via the overlay in normal (power-safe) state;
# an initramfs rebuilt onto the overlay is discarded on reboot. Refuse early.
if command -v raspi-config >/dev/null 2>&1 \
   && [ "$(raspi-config nonint get_overlay_now 2>/dev/null)" = "0" ]; then
  echo "Root fs is READ-ONLY (overlay on); the new initramfs would be lost on reboot." >&2
  echo "Run 'sudo maintenance rw' first, deploy, then 'sudo maintenance ro'." >&2
  exit 1
fi

echo ">> Rebuilding the initramfs (re-embeds the current boot splash)..."
update-initramfs -u

echo ">> Restarting the display..."
systemctl restart lms-cover-display.service

echo "Done. Reboot to verify the early (initramfs) splash now matches later screens."
