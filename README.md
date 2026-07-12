# Lyrion Cover Display

A lightweight kiosk "now playing" screen for **Lyrion Music Server (LMS)**. It
follows your LMS player(s) and draws the current **album cover** with artist /
track / album below — no desktop, no browser. Each frame is composited with
**pygame** (`SDL_VIDEODRIVER=dummy`) and copied straight to the **firmware
framebuffer** (`/dev/fb0`), light enough for a **Raspberry Pi 3 A+ (512MB)**.

| Now playing | Resting |
|:---:|:---:|
| ![Now playing](docs/now-playing.png) | ![Resting](docs/resting.png) |

- **Display only** — LMS and the audio player live elsewhere on your network.
- **Portrait stacked layout:** cover on top, washed info band below; non-square
  art is blur-filled (no black bars).
- **Multi-player:** track several players by priority, show one at a time.
- **Event-driven:** updates push from the LMS CLI (port 9090), polling fallback.
- **Powers HDMI off when idle** (`vcgencmd`), wakes on play.
- **Hi-res Spotify covers:** Spotify's CDN serves the same album art at 2000px
  under a different URL prefix. The display derives that URL itself (works
  with stock Spotty): the next track's cover is prefetched hi-res, so album
  swaps are instant *and* full quality; a manual skip paints the fast 640px
  variant immediately and crossfades to hi-res a couple of seconds later.
- **Radio covers:** for internet-radio songs (with an album tag) it looks up
  proper album art on the covers.musichoarders.xyz aggregator. Strict by
  design: only exact artist+album matches (plus punctuation variants and
  Deluxe/Single/Remastered-style editions of the same release), and when the
  station sends its own per-song artwork the found cover must also *look*
  like it (perceptual dHash check) or it is rejected. Station logos are
  detected and never used as a false reference. Everything fails soft to the
  station's own art. *Caveat: that site has no public API — the client mimics
  its web app and may stop working at any time.*
- **Crossfade upgrades:** when a sharper cover replaces art already on
  screen, it blends in (`upgrade_fade_seconds`) instead of cutting.
- **Script-aware text:** per-line font fallback to FreeSerif for scripts
  DejaVu lacks (e.g. Ethiopic artist names from radio metadata).

## Install (on the Pi)

```bash
git clone https://github.com/patapovich/lyrion-cover-display
cd lyrion-cover-display
sudo ./install.sh
```

Installs the deps (`python3-pygame python3-pil fonts-dejavu-core fonts-freefont-ttf`), the systemd
service, the boot-splash + `maintenance` helpers, and a copy of `config.ini`.
Then:

```bash
python3 lms_cover_display.py --list-players   # find your player's MAC
nano config.ini                               # set server_host (and player)
sudo systemctl start lms-cover-display        # ...or reboot
```

Logs: `journalctl -u lms-cover-display -f` (RAM-only, cleared on reboot).

## Configuration

Edit `config.ini` — full annotated list in
[`config.example.ini`](config.example.ini). The common keys:

| Key                  | Meaning                                                        |
|----------------------|----------------------------------------------------------------|
| `server_host`        | LMS hostname/IP (**required**)                                 |
| `player`             | Player MAC (recommended) or name; blank = first player         |
| `players`            | Several players, priority order (first = highest), comma/space separated; overrides `player`. Highest-priority *playing* one shows; preempts; sticky last-active when idle |
| `rotate`             | `0/90/180/270` for a portrait mount (default `90`)             |
| `background`         | `blur` (saturated, blurred, zoomed full-cover backdrop, lms-material style) or `black` |
| `idle_blank_seconds` | Hold last cover this long after stop, then power HDMI off (default `300`) |
| `upgrade_fade_seconds` | Crossfade when a sharper cover replaces art on screen; `0` = hard cut (default `0.4`) |
| `radio_cover_search` | Master switch for the radio cover lookup (default `true`)      |
| `radio_cover_country` | Store country for the search (default `de`; `fi` is rejected upstream) |
| `radio_cover_sources` | Sources in quality-preference order (default `applemusic, tidal, amazonmusic, spotify`) |
| `radio_cover_title_fallback` | Album tag missing → search the song title as an album name (default `false`, strict) |
| `radio_cover_timeout` | Wall-clock deadline per search, seconds (default `8.0`)       |
| `radio_cover_match_threshold` | Max dHash distance (0–64) vs the station's per-song art (default `16`; matches observed ≤14, rejects ≥23) |
| `radio_cover_loose_match` | Accept punctuation variants + decorated editions — only under the dHash gate (default `true`) |

Updates are event-driven via the LMS CLI (`cli_port`, default `9090`); set
`cli_user`/`cli_pass` only if your LMS has CLI auth.

## Tests

```bash
python3 -m unittest discover -s tests        # offline, no Pi/network needed
RUN_LIVE=1 python3 -m unittest discover -s tests   # + live aggregator queries
```

## Notes

- **Hardware:** Pi 3 A+ + HDMI panel (via a scaler). Native 2048×1536 isn't
  reachable on a Pi 3 (HDMI clock cap), so it feeds the scaler `1600×1200@60`
  (`cmdline.txt`) which upscales. vc4 KMS is left off so the firmware framebuffer
  persists from power-on (no signal drop at boot).
- **Production hardening:** read-only root overlay (writes vanish on reboot, so a
  power cut can't corrupt the card — edit with `sudo maintenance rw`, then
  `sudo maintenance ro`), RAM-only logs, never-give-up systemd restart, no swap.
- **Splash assets** are generated by `python3 tools/gen_splash.py`.

## Troubleshooting

- **Black screen:** check `journalctl -u lms-cover-display -f`; reboot after
  install so `video` group + disabled `getty@tty1` take effect.
- **Wrong orientation:** set `rotate = 270` (or 90/180) and restart.
- **Config edits don't stick:** the card is read-only — `sudo maintenance rw` first.
- **"No players connected":** the player must be on and connected to LMS
  (`--list-players`).
