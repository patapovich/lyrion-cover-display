# Lyrion Cover Display

A lightweight kiosk "now playing" screen for **Lyrion Music Server (LMS)**.

It follows one LMS player and draws the current **album cover** with the
artist / track / album below it — no desktop, no browser, no compositor. Each
frame is composited offscreen with **pygame** (`SDL_VIDEODRIVER=dummy`) and
copied straight to the **firmware framebuffer** (`/dev/fb0`, 32-bit) via mmap.
That keeps it light enough for a **Raspberry Pi 3 A+ (512MB RAM)** driving a
2048×1536 iPad panel over HDMI.

- **Display only** — LMS and the audio player live elsewhere on your network.
- Polls the LMS JSON-RPC HTTP API (~1×/sec); fetches a cover only when the track
  changes.
- **Portrait layout:** the cover fills the top of the (rotated) screen full-width;
  artist / track / album sit in a washed-out band below it, with a soft shadow
  cast from the cover. Non-square art that LMS letterboxes is filled with a
  blurred, edge-matched extension of the artwork (no black bars, no hard seam).
- **Unified resting screen:** when stopped / paused-idle / no player / powered
  off, it shows the splash (a vinyl disc in the cover zone with the `Lyrion` +
  status line in the same band the now-playing title uses) — boot splash, loading
  and stopped all share one design. Regenerate the splash assets with
  `python3 tools/gen_splash.py`.
- **Powers HDMI off when idle:** after `idle_blank_seconds` of stop/no-player, or
  shortly after the player is powered off, it runs `vcgencmd display_power 0` to
  actually cut the HDMI signal (not just a black fill); playing wakes it. Set
  `power_blank_enabled = false` for the legacy black-fill behavior.

## Hardware / OS

- Raspberry Pi 3 A+ (or anything with HDMI + WiFi).
- HDMI to the 2048×1536 panel (via a scaler/adapter).
- **Raspberry Pi OS Lite (Bookworm)** — boots straight to a firmware framebuffer.

When imaging with **Raspberry Pi Imager**, set the **WiFi SSID/password**,
hostname, and enable SSH in the advanced options so the Pi comes up headless.

## Install (on the Pi)

```bash
git clone <this repo>   # or copy the folder onto the Pi
cd lyrion-cover-display
sudo ./install.sh
```

`install.sh` installs the system packages (`python3-pygame python3-numpy
fonts-dejavu-core`; LMS is reached with the Python stdlib), adds the user to the
`video` group (for `/dev/fb0` + `vcgencmd`), copies `config.example.ini` →
`config.ini`, installs/enables the systemd service plus the `maintenance` and
boot-splash helpers (`splash-fb`, `splash-fb.service`, the initramfs hooks), and
disables the login console on `tty1`.

Then configure and start:

```bash
# Find your player's MAC (set server_host first):
python3 lms_cover_display.py --list-players
nano config.ini                         # set server_host (and player)
sudo systemctl start lms-cover-display  # ...or reboot
```

Logs (RAM only, cleared on reboot): `journalctl -u lms-cover-display -f`

## Configuration

Edit `config.ini` (see `config.example.ini` for the full annotated list):

| Key                  | Meaning                                                        |
|----------------------|----------------------------------------------------------------|
| `server_host`        | LMS hostname/IP (**required**)                                 |
| `server_port`        | LMS web port (default `9000`)                                  |
| `player`             | Player MAC (recommended) or name; blank = first player         |
| `players`            | Track several players, show one at a time. MACs/names in priority order (first = highest), comma/space separated; **overrides `player`**. Highest-priority *playing* player wins (preempts); when none play, sticks with the last that played. Blank = single-player via `player`. |
| `cli_port`           | LMS command-line interface port for the event stream (default `9090`). Updates are event-driven (push), with polling as a fallback when the socket is down |
| `cli_user` / `cli_pass` | LMS CLI login — only if your LMS has CLI authentication enabled (most don't) |
| `event_heartbeat`    | Safety re-sync interval (s) while the event socket is up (default `30`); `poll_interval` is the fallback cadence while it's down |
| `rotate`             | Rotate the image `0/90/180/270` for a portrait mount (default `90`; flip 90↔270 if upside-down) |
| `info_height`        | Layout: `>0` = stacked (cover as a 1:1 square at top, info band below — band height is derived, so any positive value behaves the same); `0` = legacy centered layout (default `0.30`) |
| `info_wash`          | Darkening (0–255) of the background under the info band (default `150`) |
| `idle_blank_seconds` | Hold last cover this long after stop, then blank (default `300`) |
| `background`         | `blur` (soft-blurred cover) or `black`                         |
| `cover_px`           | Cover size requested from LMS (default `1000`)                 |
| `show_album`         | Show the album line (default `true`)                           |
| `text_show_seconds`  | Centered-layout only: show text this long after a track change, then fade |
| `text_fade_seconds`  | Fade-out duration for the artist/title overlay (default `0.6`) |
| `blank_on_pause`     | Treat pause like stop for the blank/power-off timer (default `false`) |
| `power_blank_enabled`| Physically cut HDMI when idle (default `true`); `false` = legacy black-fill |
| `hdmi_off_cmd` / `hdmi_on_cmd` | Shell commands to power HDMI off/on (default `vcgencmd display_power 0`/`1`) |
| `hdmi_query_cmd`     | Shell command to read current HDMI power state (default `vcgencmd display_power`) |
| `hdmi_off_grace`     | Seconds after the player reports `power=0` before cutting HDMI (default `10`) |
| `unreachable_grace`  | Seconds to hold the last frame on a server/wifi blip before showing "connecting…" (default `15`; a sustained outage past `idle_blank_seconds` rests the panel) |

> Editing config on a locked (read-only) Pi: run `sudo maintenance rw`, edit,
> then `sudo maintenance ro` — see **Production hardening**.

## How it talks to LMS

- `POST http://<server>:9000/jsonrpc.js` with
  `{"method":"slim.request","params":["<MAC>",["status","-",1,"tags:aclKxN"]]}`.
- Current track is in `playlist_loop[0]` (`title`, `artist`, `album`, `coverid`,
  `artwork_url`); `mode` is `play`/`pause`/`stop`.
- Cover source: prefer `artwork_url` when present (internet radio/remote set a
  synthetic negative `coverid` that does *not* resolve via `/music/...`). Local
  tracks and Spotify use `http://<server>:9000/music/<coverid>/cover_1000x1000.jpg`.

## Display & hardware notes (this build)

- **Native 2048×1536 isn't reachable on a Pi 3.** The panel's native timing needs
  a ~205 MHz pixel clock; the Pi 3's HDMI caps ~162 MHz. We feed the scaler
  **1600×1200@60** (forced in `cmdline.txt`: `video=HDMI-A-1:1600x1200@60e`) and
  it upscales to the panel. The framebuffer is fixed landscape, so portrait is
  done by rotating the composed image in software before scanout.
- **Rendering is direct framebuffer.** pygame composes offscreen
  (`SDL_VIDEODRIVER=dummy`) and we mmap-copy the frame to `/dev/fb0`; vc4 KMS is
  left disabled, so the firmware framebuffer persists from power-on with no
  modeset and the HDMI signal never drops during boot. (SDL's KMSDRM/GL path goes
  black on this VC4 + adapter, which is why we bypass it.)
- **WiFi:** prefer the 5 GHz band if your 2.4 GHz network proves unreliable.

## Production hardening

The deployed kiosk is locked down to survive unclean power-offs and minimize SD
wear:

- **Read-only root (overlay filesystem).** The card is mounted read-only; all
  writes go to a RAM overlay and vanish on reboot, so pulling power can't corrupt
  the card. Toggle with the installed helper:
  - `sudo maintenance status` — show overlay state
  - `sudo maintenance rw` — reboot to a **writable** card (to edit config)
  - `sudo maintenance ro` — reboot back to the **locked** state
  (The boot partition stays writable so `cmdline.txt`/`config.txt` are always
  editable for recovery.)
- **RAM-only logging.** `journald` is `Storage=volatile` (logs live in `/run`,
  viewable with `journalctl` until reboot); no swap file.
- **Fast boot.** The service doesn't wait for the network (it retries LMS
  itself), and Bluetooth/audio/ModemManager/avahi/triggerhappy plus the apt /
  man-db / e2scrub / fstrim timers are disabled. Boots in ~15s.

## Troubleshooting

- **Black screen / nothing on the panel.** `journalctl -u lms-cover-display -f`.
  A common cause is framebuffer permission — reboot after install so `video`
  group membership applies, and ensure `getty@tty1` is disabled (it owns the
  console otherwise). The renderer logs the framebuffer geometry on startup.
- **Picture upside-down / wrong orientation.** Set `rotate = 270` (or 90/180) and
  restart. On a locked Pi: `sudo maintenance rw`, edit, `sudo maintenance ro`.
- **Config edits don't stick.** The card is read-only; use `sudo maintenance rw`
  first (see above).
- **"No players connected."** The player must be on and connected to LMS; verify
  with `--list-players`.
