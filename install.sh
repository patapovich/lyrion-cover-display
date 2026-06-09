#!/usr/bin/env bash
# Install Lyrion Cover Display as a systemd service on Raspberry Pi OS Lite.
# Run from the project directory:  sudo ./install.sh
set -euo pipefail

APPDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_SRC="$APPDIR/systemd/lms-cover-display.service"
SERVICE_DST="/etc/systemd/system/lms-cover-display.service"

# Determine the unprivileged user the app should run as (the one who invoked sudo).
RUN_USER="${SUDO_USER:-$(id -un)}"
if [ "$RUN_USER" = "root" ]; then
  echo "Refusing to run the display as root. Run: sudo ./install.sh (as a normal user)." >&2
  exit 1
fi

if [ "$(id -u)" -ne 0 ]; then
  echo "Please run with sudo: sudo ./install.sh" >&2
  exit 1
fi

echo ">> Installing system packages..."
apt-get update
apt-get install -y python3-pygame python3-numpy fonts-dejavu-core

echo ">> Adding $RUN_USER to the video group (for /dev/fb0 + vcgencmd access)..."
usermod -aG video "$RUN_USER"

if [ ! -f "$APPDIR/config.ini" ]; then
  echo ">> Creating config.ini from template (edit it: server_host + player)."
  cp "$APPDIR/config.example.ini" "$APPDIR/config.ini"
  chown "$RUN_USER":"$RUN_USER" "$APPDIR/config.ini"
fi
# Restrict config: it may hold cli_pass (LMS CLI auth). Owner-only, every run.
chmod 0600 "$APPDIR/config.ini"

echo ">> Installing systemd service..."
sed -e "s|__USER__|$RUN_USER|g" -e "s|__APPDIR__|$APPDIR|g" \
  "$SERVICE_SRC" > "$SERVICE_DST"

echo ">> Installing the maintenance (read-only overlay) helper..."
install -m 0755 "$APPDIR/scripts/maintenance.sh" /usr/local/sbin/maintenance

echo ">> Installing the boot-splash helper + service..."
sed "s|__APPDIR__|$APPDIR|g" "$APPDIR/scripts/splash-fb.sh" > /usr/local/sbin/splash-fb
chmod 0755 /usr/local/sbin/splash-fb
cp "$APPDIR/systemd/splash-fb.service" /etc/systemd/system/splash-fb.service

echo ">> Installing the initramfs boot-splash hooks..."
sed "s|__APPDIR__|$APPDIR|g" "$APPDIR/initramfs/hooks/coversplash" \
  > /etc/initramfs-tools/hooks/coversplash
chmod 0755 /etc/initramfs-tools/hooks/coversplash
install -m 0755 "$APPDIR/initramfs/scripts/init-premount/coversplash" \
  /etc/initramfs-tools/scripts/init-premount/coversplash
update-initramfs -u

echo ">> Disabling getty on tty1 (the display owns that console)..."
systemctl disable getty@tty1.service || true

echo ">> Configuring journald for RAM-only, size-capped logging..."
mkdir -p /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/lyrion.conf <<'JCONF'
# RAM-only logs (the root fs is a read-only overlay in production) with a hard
# cap so a chatty crash loop can't exhaust the tmpfs.
[Journal]
Storage=volatile
RuntimeMaxUse=32M
RuntimeMaxFileSize=8M
JCONF
systemctl restart systemd-journald || true

systemctl daemon-reload
systemctl enable lms-cover-display.service
systemctl enable splash-fb.service

cat <<EOF

Done. Next steps:
  1. Edit config:   nano "$APPDIR/config.ini"   (set server_host, and player)
     Find the player MAC with:
       python3 "$APPDIR/lms_cover_display.py" --list-players
  2. Start it now:  sudo systemctl start lms-cover-display
     Or reboot to launch it automatically on boot.
  3. Logs:          journalctl -u lms-cover-display -f

Note: group membership (video/render/input) takes effect after a reboot.
EOF
