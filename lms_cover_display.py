#!/usr/bin/env python3
"""Lyrion Cover Display — a lightweight kiosk "now playing" album-art screen.

Polls a Lyrion Music Server (LMS) over its JSON-RPC HTTP API, follows one
player, and draws the current album cover (with a small artist/title overlay)
straight to the firmware framebuffer (/dev/fb0): pygame composes an offscreen
surface (SDL_VIDEODRIVER=dummy) which we mmap-copy to the framebuffer ourselves.
No desktop, no browser — built to run comfortably on a 512MB Raspberry Pi 3 A+.

Usage:
    python3 lms_cover_display.py [--config PATH] [--windowed] [--list-players]

See config.example.ini for configuration.
"""
from __future__ import annotations

import argparse
import configparser
import io
import json
import os
import select
import shlex
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import quote, unquote

# Network errors worth retrying on (urllib wraps socket/timeout errors in
# URLError; HTTPError is a URLError; socket.timeout is an OSError; a malformed
# JSON body raises a ValueError). RuntimeError = our own "no players" signal.
NET_ERRORS = (urllib.error.URLError, OSError)

# pygame is imported lazily in run() so that --list-players works on a headless
# box without a display/SDL, and so SDL_VIDEODRIVER can be set beforehand.


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

DEFAULTS = {
    "server_host": "",
    "server_port": "9000",
    "player": "",            # single player: MAC (preferred) or name; blank = first player
    "players": "",           # OR several players, priority order (first = highest), comma/space separated; overrides `player`
    "poll_interval": "1.0",  # fallback sweep interval while the event socket is down
    # Event-driven updates: subscribe to the LMS CLI (port 9090) push stream
    # instead of polling. cli_user/cli_pass only if your LMS has CLI auth set.
    "cli_port": "9090",      # LMS command-line interface port (event stream)
    "cli_user": "",          # CLI login user (blank = no auth, the common case)
    "cli_pass": "",          # CLI login password
    "event_heartbeat": "30.0",  # safety re-sweep interval while the socket is up
    "cover_px": "1000",      # requested cover size from LMS (server-side resize)
    "idle_blank_seconds": "300",   # hold last cover this long after stop, then blank
    "text_show_seconds": "8",      # show artist/title this long after a track change (0 = always)
    "text_fade_seconds": "0.6",    # fade-out duration for the artist/title overlay
    "blank_on_pause": "false",     # treat pause like stop for the blank timer
    "background": "blur",          # "blur" | "black"
    "rotate": "90",                # rotate the rendered image: 0|90|180|270 (panel mounted portrait)
    "info_height": "0.30",         # >0 = stacked layout (cover as a 1:1 square at top, info band below); 0 = legacy centered. In portrait the band height is derived (ch-cw), so the value only selects the layout.
    "info_wash": "0",              # extra darkening alpha (0-255) under the info band; 0 = none (the uniform backdrop tint already dims evenly; text has its own shadow)
    "show_album": "true",          # include album line in the overlay
    "request_timeout": "5.0",      # HTTP timeout (seconds)
    # When idle/powered-off, physically power the HDMI output off (not just a
    # black fill). The cold scaler re-locks slowly (~8-9s) so this is reserved
    # for genuine idle; the wake path re-paints the splash before the signal.
    "power_blank_enabled": "true", # false = legacy black-fill, never drop HDMI
    "hdmi_off_cmd": "vcgencmd display_power 0",  # shell cmd to power HDMI OFF
    "hdmi_on_cmd": "vcgencmd display_power 1",    # shell cmd to power HDMI ON
    "hdmi_query_cmd": "vcgencmd display_power",   # reads current state (…=0/1)
    "hdmi_off_grace": "10.0",      # secs after player power=0 before HDMI off
    "unreachable_grace": "15.0",   # secs to hold the last frame on a server/wifi blip before showing "connecting…"
}


@dataclass
class Config:
    server_host: str
    server_port: int
    player: str
    players: list[str]        # priority order, first = highest; empty = follow `player`
    poll_interval: float
    cli_port: int
    cli_user: str
    cli_pass: str
    event_heartbeat: float
    cover_px: int
    idle_blank_seconds: float
    text_show_seconds: float
    text_fade_seconds: float
    blank_on_pause: bool
    background: str
    rotate: int
    info_height: float
    info_wash: int
    show_album: bool
    request_timeout: float
    power_blank_enabled: bool
    hdmi_off_cmd: str
    hdmi_on_cmd: str
    hdmi_query_cmd: str
    hdmi_off_grace: float
    unreachable_grace: float

    @property
    def base_url(self) -> str:
        return f"http://{self.server_host}:{self.server_port}"


def load_config(path: str | None) -> Config:
    parser = configparser.ConfigParser()
    parser.read_dict({"lms": DEFAULTS})
    if path:
        if not os.path.exists(path):
            sys.exit(f"Config file not found: {path}")
        parser.read(path)
    s = parser["lms"]

    host = s.get("server_host", "").strip()
    if not host:
        sys.exit(
            "server_host is not set. Copy config.example.ini to config.ini and set "
            "server_host (and optionally player)."
        )

    # A typo'd number/bool in config.ini raises ValueError here; turn that into a
    # friendly exit instead of a traceback (with never-give-up restart, an ugly
    # startup crash would otherwise loop forever).
    try:
        rotate = s.getint("rotate") % 360
        if rotate not in (0, 90, 180, 270):
            sys.exit(f"rotate must be one of 0/90/180/270 (got {rotate}).")
        background = s.get("background", "blur").strip().lower()
        if background not in ("blur", "black"):
            print(f"[warn] background '{background}' invalid; using 'blur'.",
                  flush=True)
            background = "blur"
        return Config(
            server_host=host,
            server_port=s.getint("server_port"),
            player=s.get("player", "").strip(),
            # `players` (comma/space separated, priority order) supersedes `player`.
            # Empty list = legacy single-player path (follow `player`, blank = first).
            players=[p for p in s.get("players", "").replace(",", " ").split() if p],
            poll_interval=max(0.1, s.getfloat("poll_interval")),  # >0 avoids a busy-loop
            cli_port=s.getint("cli_port"),
            cli_user=s.get("cli_user", "").strip(),
            cli_pass=s.get("cli_pass", "").strip(),
            event_heartbeat=max(1.0, s.getfloat("event_heartbeat")),
            cover_px=max(1, s.getint("cover_px")),               # >=1 keeps the URL valid
            idle_blank_seconds=max(0.0, s.getfloat("idle_blank_seconds")),
            text_show_seconds=max(0.0, s.getfloat("text_show_seconds")),
            text_fade_seconds=max(0.0, s.getfloat("text_fade_seconds")),
            blank_on_pause=s.getboolean("blank_on_pause"),
            background=background,
            rotate=rotate,
            info_height=max(0.0, min(0.9, s.getfloat("info_height"))),
            info_wash=max(0, min(255, s.getint("info_wash"))),
            show_album=s.getboolean("show_album"),
            request_timeout=s.getfloat("request_timeout"),
            power_blank_enabled=s.getboolean("power_blank_enabled"),
            hdmi_off_cmd=s.get("hdmi_off_cmd", "").strip(),
            hdmi_on_cmd=s.get("hdmi_on_cmd", "").strip(),
            hdmi_query_cmd=s.get("hdmi_query_cmd", "").strip(),
            hdmi_off_grace=max(0.0, s.getfloat("hdmi_off_grace")),
            unreachable_grace=max(0.0, s.getfloat("unreachable_grace")),
        )
    except ValueError as exc:
        sys.exit(f"Invalid value in config.ini: {exc}")


# --------------------------------------------------------------------------- #
# LMS JSON-RPC client
# --------------------------------------------------------------------------- #

class LMSClient:
    MAX_JSON_BYTES = 10 * 1024 * 1024      # status JSON is tiny; cap defends the 512MB Pi

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.rpc_url = f"{cfg.base_url}/jsonrpc.js"
        self._opener = urllib.request.build_opener()   # follows redirects

    def request(self, player: str, command: list) -> dict:
        payload = {"id": 1, "method": "slim.request", "params": [player, command]}
        req = urllib.request.Request(
            self.rpc_url, data=json.dumps(payload).encode("utf-8"),
            # identity: refuse gzip so the body is always plain JSON (some LMS
            # setups otherwise return gzipped replies that fail UTF-8 decode).
            headers={"Content-Type": "application/json",
                     "Accept-Encoding": "identity"},
        )
        with self._opener.open(req, timeout=self.cfg.request_timeout) as resp:
            body = resp.read(self.MAX_JSON_BYTES + 1)   # HTTPError raised on !2xx
        if len(body) > self.MAX_JSON_BYTES:
            raise ValueError("LMS response too large")
        return json.loads(body).get("result", {})

    MAX_IMAGE_BYTES = 32 * 1024 * 1024     # cap so a runaway remote image can't OOM the Pi

    def get_bytes(self, url: str) -> bytes:
        """Fetch raw bytes (cover image). Follows the imageproxy 301 redirect.
        Bounded read: a remote artwork_url could be arbitrarily large, and this
        runs on a 512MB Pi with no swap."""
        with self._opener.open(url, timeout=self.cfg.request_timeout) as resp:
            data = resp.read(self.MAX_IMAGE_BYTES + 1)
            if len(data) > self.MAX_IMAGE_BYTES:
                raise ValueError(f"cover exceeds {self.MAX_IMAGE_BYTES} bytes")
            return data

    def list_players(self) -> list[dict]:
        result = self.request("-", ["players", "0", 50])
        return result.get("players_loop", [])

    def resolve_player(self, wanted: str) -> str:
        """Resolve the configured player (MAC or name, or blank) to a MAC."""
        players = self.list_players()
        if not players:
            raise RuntimeError("No players are connected to LMS.")

        if not wanted:
            return players[0]["playerid"]

        low = wanted.lower()
        for p in players:
            if p.get("playerid", "").lower() == low:
                return p["playerid"]
        for p in players:
            if p.get("name", "").lower() == low:
                return p["playerid"]
        raise RuntimeError(
            f"Player {wanted!r} not found. Known players: "
            + ", ".join(f"{p.get('name')} ({p.get('playerid')})" for p in players)
        )

    def resolve_players(self, specs: list[str]) -> list[str]:
        """Resolve configured players (MACs/names, priority order) to the MACs of
        the currently-connected players, de-duplicated, priority preserved. An
        empty `specs` means 'follow the first connected player' (legacy blank).
        Specs that aren't connected right now are skipped (retried next poll, as a
        player may connect after boot). Network failure propagates to the caller."""
        players = self.list_players()
        if not players:
            return []
        if not specs:
            return [players[0]["playerid"]]
        by_id = {p.get("playerid", "").lower(): p["playerid"] for p in players}
        by_name = {p.get("name", "").lower(): p["playerid"] for p in players}
        out: list[str] = []
        for spec in specs:
            mac = by_id.get(spec.lower()) or by_name.get(spec.lower())
            if mac and mac not in out:
                out.append(mac)
        return out

    def status(self, player: str) -> dict:
        # "-" = current track index; 1 item; tags for the fields we render.
        # a=artist l=album c=coverid K=artwork_url x=remote N=remote title
        return self.request(player, ["status", "-", 1, "tags:aclKxN"])


# --------------------------------------------------------------------------- #
# Event listener — LMS CLI push notifications (port 9090), replaces polling
# --------------------------------------------------------------------------- #

class EventListener:
    """A persistent connection to the LMS command-line interface that subscribes
    to player state-change notifications, so the render loop is woken by a push
    instead of polling. We subscribe to a narrow event set; ANY received line is
    treated as 'something changed, re-query' (the loop then does its normal HTTP
    status sweep) — we never parse event semantics, which keeps this robust to the
    exact event vocabulary. Falls back to polling while disconnected, and
    reconnects with capped backoff."""

    # Subscribe to just the state-relevant notifications (not mixer/time, which
    # would fire constantly). newsong/pause/stop cover play state; power covers
    # on/off; client covers a player (dis)connecting.
    SUBSCRIBE = "subscribe playlist,power,client"
    BACKOFF = (1.0, 2.0, 5.0, 10.0, 30.0)
    MAX_LINE = 8192            # drop+reconnect if a line grows past this (no \n)

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._sock = None
        self._buf = b""
        self._fails = 0
        self._next_try = 0.0       # monotonic; earliest next reconnect attempt

    @property
    def connected(self) -> bool:
        return self._sock is not None

    def fileno(self) -> int:
        return self._sock.fileno() if self._sock is not None else -1

    def connect(self) -> bool:
        """Open + subscribe (brief blocking handshake), then go non-blocking.
        Returns True on success; safe to call when already connected."""
        if self._sock is not None:
            return True
        sock = None
        try:
            sock = socket.create_connection(
                (self.cfg.server_host, self.cfg.cli_port),
                timeout=self.cfg.request_timeout)
            sock.settimeout(self.cfg.request_timeout)
            if self.cfg.cli_user or self.cfg.cli_pass:
                sock.sendall(
                    f"login {quote(self.cfg.cli_user)} "
                    f"{quote(self.cfg.cli_pass)}\n".encode())
                sock.recv(4096)                       # consume the login echo
            sock.sendall((self.SUBSCRIBE + "\n").encode())
            sock.setblocking(False)
            self._sock, self._buf, self._fails = sock, b"", 0
            print(f"Event socket connected ({self.cfg.server_host}:"
                  f"{self.cfg.cli_port}); subscribed.", flush=True)
            return True
        except OSError as exc:
            if sock is not None:
                try:
                    sock.close()                      # don't leak the fd
                except OSError:
                    pass
            self._fails += 1
            self._next_try = time.monotonic() + self.BACKOFF[
                min(self._fails - 1, len(self.BACKOFF) - 1)]
            print(f"[warn] event socket connect failed ({exc}); polling, "
                  f"retry in {self._next_try - time.monotonic():.0f}s", flush=True)
            return False

    def try_reconnect(self, now: float) -> bool:
        """Attempt a reconnect if the backoff window has elapsed."""
        if self._sock is not None or now < self._next_try:
            return False
        return self.connect()

    def drain(self) -> tuple[list[str], bool]:
        """Read everything pending without blocking. Returns (lines, alive);
        alive=False on EOF/error (caller should mark the socket down). Splitting
        on b"\\n" before decoding keeps multibyte UTF-8 intact across recv chunks."""
        lines: list[str] = []
        try:
            while True:
                chunk = self._sock.recv(4096)
                if chunk == b"":
                    self.mark_down()
                    return lines, False
                self._buf += chunk
                while b"\n" in self._buf:
                    raw, self._buf = self._buf.split(b"\n", 1)
                    lines.append(unquote(raw.decode("utf-8", "replace")).strip())
                # A newline-less stream must not grow the buffer without bound
                # (would OOM over long uptime): force a clean reconnect instead.
                if len(self._buf) > self.MAX_LINE:
                    print("[warn] event line exceeded "
                          f"{self.MAX_LINE}B with no newline; reconnecting.",
                          flush=True)
                    self.mark_down()
                    return lines, False
        except BlockingIOError:
            return lines, True                        # nothing more to read
        except OSError:
            self.mark_down()
            return lines, False

    def mark_down(self):
        # Close and schedule an immediate reconnect attempt; a failed connect()
        # then arms the escalating backoff (so we don't double-count failures).
        self.close()
        self._next_try = time.monotonic()

    def close(self):
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
            self._buf = b""


# --------------------------------------------------------------------------- #
# Now-playing model
# --------------------------------------------------------------------------- #

@dataclass
class NowPlaying:
    mode: str = "stop"           # play | pause | stop
    title: str = ""
    artist: str = ""
    album: str = ""
    cover_key: str = ""          # identity used to detect art changes
    cover_url: str = ""          # absolute URL to fetch the cover from

    @staticmethod
    def parse(cfg: Config, status: dict) -> "NowPlaying":
        np = NowPlaying(mode=status.get("mode", "stop"))
        loop = status.get("playlist_loop") or []
        if not loop:
            return np
        track = loop[0]
        np.title = track.get("title", "") or track.get("remote_title", "")
        np.artist = track.get("artist", "") or track.get("albumartist", "")
        np.album = track.get("album", "")

        coverid = track.get("coverid")
        artwork_url = track.get("artwork_url")
        px = cfg.cover_px

        # Prefer artwork_url when present. Remote sources (internet radio, some
        # streams) set a synthetic/negative coverid that does NOT resolve via
        # /music/<id>/cover, but DO provide a real artwork_url (often an
        # /imageproxy/... link that 301-redirects to the real image). Local
        # tracks and Spotify albums have no artwork_url, so they fall to coverid.
        if artwork_url:
            np.cover_key = f"url:{artwork_url}"
            if artwork_url.startswith("http"):
                np.cover_url = artwork_url
            else:
                np.cover_url = f"{cfg.base_url}/{artwork_url.lstrip('/')}"
        elif coverid:
            np.cover_key = f"cid:{coverid}"
            # The _o suffix = keep the artwork's native aspect, max dimension px,
            # no square pad/crop (plain cover_NxN.jpg squares it). We scale and
            # blur-fill ourselves, so we want the original aspect ratio.
            np.cover_url = f"{cfg.base_url}/music/{quote(str(coverid))}/cover_{px}x{px}_o.jpg"
        else:
            # Last resort: the "current cover" shortcut for this player.
            np.cover_key = "current"
            pid = status.get("playerid", "")
            np.cover_url = (
                f"{cfg.base_url}/music/current/cover_{px}x{px}_o.jpg?player={quote(pid)}"
            )
        return np


# --------------------------------------------------------------------------- #
# Renderer (pygame)
# --------------------------------------------------------------------------- #

def smoothstep(t: float) -> float:
    """Hermite ease 3t²−2t³ on [0,1]: zero slope at both ends, softer than linear."""
    return t * t * (3 - 2 * t)


class Display:
    """Renders the now-playing frame.

    We compose each frame on an offscreen pygame surface and copy it straight to
    the firmware framebuffer (/dev/fb0, 32-bit). vc4 KMS is disabled so the
    firmware's framebuffer persists from power-on with no modeset — the HDMI
    signal never drops during boot (important on this slow HDMI scaler), so a
    boot splash can stay continuous until the cover. The framebuffer is fixed
    landscape; for a portrait mount we compose on a rotated canvas and rotate at
    present time.
    """

    def __init__(self, cfg: Config):
        import pygame  # local import: SDL_VIDEODRIVER must be set first

        self.pygame = pygame
        self.cfg = cfg
        pygame.init()
        try:
            pygame.mouse.set_visible(False)
        except pygame.error:
            pass

        self.rot = cfg.rotate          # already normalized to 0/90/180/270 in load_config
        self._open_framebuffer()
        # The framebuffer is fixed landscape (self.w x self.h). For a 90/270
        # rotation we compose on a portrait canvas and rotate at present time.
        if self.rot in (90, 270):
            self.cw, self.ch = self.h, self.w
        else:
            self.cw, self.ch = self.w, self.h
        self.screen = pygame.Surface((self.cw, self.ch))
        # Stacked layout (info_height > 0): show the cover as a 1:1 square zone
        # filling the canvas width at the top; the info band takes whatever height
        # is left below it. info_height == 0 keeps the legacy centered layout.
        if cfg.info_height > 0 and self.ch > self.cw:
            self._info_h = self.ch - self.cw
        else:
            self._info_h = int(self.ch * cfg.info_height)
        self.blanked = False
        self._init_fonts()
        self._statusbg = self._load_statusbg()
        self._status_key = None

        # Paint the "loading" status immediately, matching the boot splash, so the
        # panel keeps showing it (the boot splash drew the same image earlier)
        # until the app has a real state to show.
        self.status_screen("loading…")
        print(f"Renderer: fb @ {self.w}x{self.h}x{self.bpp} "
              f"(canvas {self.cw}x{self.ch}, rotate {self.rot})", flush=True)

    def _open_framebuffer(self, dev="/dev/fb0"):
        """Map the firmware framebuffer (/dev/fb0) for direct writes. It is set
        up by the firmware (1600x1200x32) and persists for the whole session —
        no modeset, so the HDMI output never drops."""
        import mmap
        import numpy as np
        self.np = np

        def _read(name, default=""):
            try:
                with open("/sys/class/graphics/fb0/" + name) as fh:
                    return fh.read().strip()
            except OSError:
                return default

        w, h = _read("virtual_size", "1600,1200").split(",")
        self.w, self.h = int(w), int(h)
        self.bpp = int(_read("bits_per_pixel", "32"))
        self.stride = int(_read("stride", str(self.w * (self.bpp // 8))))
        self._fbfd = os.open(dev, os.O_RDWR)
        self._fb = mmap.mmap(self._fbfd, self.stride * self.h)
        self._unbind_fbcon()

    def _unbind_fbcon(self):
        """Detach the framebuffer text console so it never repaints over our
        frames (otherwise fbcon clears the splash/cover). Best-effort."""
        import glob
        for v in glob.glob("/sys/class/vtconsole/vtcon*"):
            try:
                with open(v + "/name") as fh:
                    if "frame buffer" not in fh.read().lower():
                        continue
                with open(v + "/bind", "w") as fh:
                    fh.write("0")
            except OSError:
                pass

    def _load_statusbg(self):
        """Load the status background (the splash artwork without a status line;
        the live status text is drawn on top by status_screen)."""
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "assets", "splashbg.png")
        if not os.path.exists(path):
            return None
        try:
            img = self.pygame.image.load(path)
            surf = self.pygame.Surface(img.get_size())
            surf.blit(img, (0, 0))
            if surf.get_size() != (self.cw, self.ch):
                surf = self.pygame.transform.smoothscale(surf, (self.cw, self.ch))
            return surf
        except (self.pygame.error, OSError):
            return None

    def status_screen(self, msg):
        """Paint the splash backdrop with the brand wordmark + a live status line
        ('loading…', 'stopped', 'paused', …) stacked in the info band — the same
        zone, fonts and drop-shadow as the now-playing title/artist, so the boot
        splash and every resting screen read as one design with the cover view.
        splashbg.png supplies the disc + gradient + band wash; we add the text.
        Only redraws when the message changes."""
        if self._status_key == msg and not self.blanked:
            return False
        self.wake()
        self._status_key = msg
        if self._statusbg is not None:
            self.screen.blit(self._statusbg, (0, 0))
        else:
            self.screen.fill((8, 8, 10))
        # Brand headline (like a title) over the live status (like the artist).
        lines = [(self.font_title, "Lyrion", (224, 226, 232))]
        if msg:
            lines.append((self.font_sub, msg, (170, 176, 190)))
        rendered = [(f.render(t, True, c), f.render(t, True, (0, 0, 0)))
                    for f, t, c in lines]
        pad = max(16, self.ch // 48)
        total = sum(fg.get_height() for fg, _ in rendered) + pad * (len(rendered) - 1)
        if self._info_h > 0:
            y = (self.ch - self._info_h) + (self._info_h - total) // 2
        else:
            y = int(self.ch * 0.82) - total // 2
        for fg, shadow in rendered:
            x = (self.cw - fg.get_width()) // 2
            self.screen.blit(shadow, (x + 2, y + 2))
            self.screen.blit(fg, (x, y))
            y += fg.get_height() + pad
        self.present()
        return True

    def _init_fonts(self):
        pygame = self.pygame
        font_candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            os.path.join(os.path.dirname(__file__), "assets", "DejaVuSans-Bold.ttf"),
        ]
        regular_candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            os.path.join(os.path.dirname(__file__), "assets", "DejaVuSans.ttf"),
        ]
        base = max(20, self.ch // 28)
        self.font_title = self._load_font(font_candidates, base)
        self.font_sub = self._load_font(regular_candidates, int(base * 0.7))
        self.font_status = self._load_font(regular_candidates, base)  # status line

    def _load_font(self, candidates, size):
        pygame = self.pygame
        for path in candidates:
            if os.path.exists(path):
                return pygame.font.Font(path, size)
        return pygame.font.Font(None, size)  # pygame's built-in fallback

    # -- cover decoding/scaling ------------------------------------------- #

    def decode_cover(self, data: bytes):
        pygame = self.pygame
        img = pygame.image.load(io.BytesIO(data))
        # Normalise to a plain 32-bit surface by blitting onto a fresh one.
        # (Surface.convert() needs a display surface, which we don't have in
        # framebuffer mode.) We fetch the artwork at its native aspect ratio
        # (cover_NxN_o.jpg), so it is shown whole, scaled to fit a 1:1 square zone;
        # the blurred/saturated backdrop (see _background) shows through any margin.
        surf = pygame.Surface(img.get_size())
        surf.blit(img, (0, 0))
        return surf

    def _scaled_cover(self, cover):
        pygame = self.pygame
        x, y, w, h = self._cover_geom(cover)
        if w == 0 or h == 0:
            return cover, (0, 0)
        return pygame.transform.smoothscale(cover, (w, h)), (x, y)

    def _cover_geom(self, cover):
        """(x, y, w, h) to show the cover inside a 1:1 square zone (side = the
        canvas width) at the top: scale to fit keeping aspect (no crop) and centre
        it within the square. The blurred background fills any margin, so the
        cover block reads as a full 1:1 square."""
        iw, ih = cover.get_size()
        if iw == 0 or ih == 0:
            return 0, 0, 0, 0
        side = min(self.cw, self.ch)
        scale = min(side / iw, side / ih)
        w = max(1, round(iw * scale))
        h = max(1, round(ih * scale))
        return (self.cw - w) // 2, (side - h) // 2, w, h

    def _band_top(self, cover):
        """Y where the info band begins = the bottom of the 1:1 square cover zone.
        Falls back to the configured info_height band when there is no cover."""
        if cover is None:
            return self.ch - self._info_h
        return min(self.cw, self.ch)

    def _saturate(self, surf, s):
        """Apply CSS `filter: saturate(s)` exactly — the W3C/SVG saturate matrix
        (Rec.709 luma coefficients 0.213 / 0.715 / 0.072)."""
        np = self.np
        a = self.pygame.surfarray.array3d(surf).astype(np.float32)
        r, g, b = a[..., 0], a[..., 1], a[..., 2]
        out = np.empty_like(a)
        out[..., 0] = (0.213 + 0.787 * s) * r + (0.715 - 0.715 * s) * g + (0.072 - 0.072 * s) * b
        out[..., 1] = (0.213 - 0.213 * s) * r + (0.715 + 0.285 * s) * g + (0.072 - 0.072 * s) * b
        out[..., 2] = (0.213 - 0.213 * s) * r + (0.715 - 0.715 * s) * g + (0.072 + 0.928 * s) * b
        out = np.clip(out, 0, 255).astype(np.uint8)
        res = self.pygame.Surface(surf.get_size())
        self.pygame.surfarray.blit_array(res, out)
        return res

    def _crop_fill(self, src, w, h):
        """Scale `src` to COVER (w, h) keeping aspect (crop the overflow), anchored
        centre-x / top — i.e. CSS `background-size:cover; position:center top`."""
        ss = self.pygame.transform.smoothscale
        iw, ih = src.get_size()
        if iw == 0 or ih == 0:
            return self.pygame.Surface((w, h))
        s = max(w / iw, h / ih)
        sw, sh = max(1, round(iw * s)), max(1, round(ih * s))
        scaled = ss(src, (sw, sh))
        return scaled.subsurface(((sw - w) // 2, 0, w, h)).copy()

    def _blur_up(self, thumb, w, h):
        """Upscale a tiny thumbnail to (w, h) in repeated ×2 bilinear steps — a
        cheap gaussian-like blur (compounding interpolation avoids blocky facets).
        The thumbnail's size sets the effective blur radius."""
        ss = self.pygame.transform.smoothscale
        out = thumb
        bw, bh = thumb.get_width() * 2, thumb.get_height() * 2
        while bw < w or bh < h:
            out = ss(out, (min(bw, w), min(bh, h)))
            bw *= 2
            bh *= 2
        return ss(out, (w, h))

    # Backdrop matching lms-material's now-playing: the cover scaled to fill,
    # `saturate(3)` then a heavy blur, `scale(1.35)` zoom, and a translucent dark
    # Backdrop = lms-material `.np-full .np-bgnd-cover`: `saturate(3)` (exact CSS
    # matrix), a heavy blur, `transform: scale(1.35)`, and a dim. We dim by
    # MULTIPLYING brightness (keeps the cover's hue — lms's "colour from cover"
    # theme stays hued, unlike a neutral-grey veil which goes muddy). Recomputed
    # only when the cover changes (cached by the cover surface).
    _BG_SAT = 3.0
    _BG_ZOOM = 1.35
    _BG_DARKEN = 0.5                            # multiply brightness (hue-preserving)
    # Blur from lms's value: `--np-full-bgnd-filter-size` is 35px at the phone
    # breakpoint (<800px), applied to a ~680px portrait viewport. The downscale
    # thumbnail width = viewport/filter encodes that same blur-to-width ratio;
    # scale(1.35) then magnifies it exactly as lms's transform does.
    _LMS_FILTER_PX = 35
    _LMS_VIEWPORT_W = 680

    def _background(self, cover):
        pygame = self.pygame
        if self.cfg.background == "black" or cover is None:
            bg = pygame.Surface((self.cw, self.ch))
            bg.fill((8, 8, 10))
            return bg
        if getattr(self, "_bg_for", None) is cover:
            return self._bg_surf                # cached: cover unchanged
        cw, ch = self.cw, self.ch
        base = self._crop_fill(cover, cw, ch)
        # Saturate THEN blur (CSS filter order). Thumbnail width = viewport/filter
        # (lms blur-to-width ratio); blur is the upscale back from this thumbnail.
        tw = max(6, round(self._LMS_VIEWPORT_W / self._LMS_FILTER_PX))
        th = max(6, round(tw * ch / cw))
        thumb = self._saturate(pygame.transform.smoothscale(base, (tw, th)),
                               self._BG_SAT)
        zw, zh = round(cw * self._BG_ZOOM), round(ch * self._BG_ZOOM)
        bg = self._blur_up(thumb, zw, zh)
        bg = bg.subsurface(((zw - cw) // 2, (zh - ch) // 2, cw, ch)).copy()  # scale(1.35)
        # Dim by multiplying brightness — preserves hue (no grey wash).
        g = int(self._BG_DARKEN * 255)
        bg.fill((g, g, g), special_flags=pygame.BLEND_MULT)
        self._bg_for, self._bg_surf = cover, bg
        return bg

    def _cover_shadow(self, band_top):
        """A soft drop shadow cast *down* from the cover's bottom edge onto the
        info band, so the cover reads as floating above the band. Darkest right
        at the edge, fading to nothing over a short ramp. Cached per band_top."""
        key = (band_top, self.cw, self.ch)
        if getattr(self, "_shadow_key", None) == key:
            return self._shadow_surf, band_top
        pygame = self.pygame
        maxa = 130                                  # contact darkness at the edge
        length = max(1, int(self.ch * 0.06))        # fade distance below the edge
        h = min(length, self.ch - band_top)
        surf = pygame.Surface((self.cw, max(1, h)), pygame.SRCALPHA)
        for yy in range(h):
            t = yy / length
            a = int(maxa * (1 - t) * (1 - t))       # quadratic falloff (soft)
            pygame.draw.line(surf, (0, 0, 0, a), (0, yy), (self.cw, yy))
        self._shadow_key, self._shadow_surf = key, surf
        return surf, band_top

    def _wash_overlay(self, band_top):
        """A soft vertical gradient that washes out the info band. It starts a
        little *above* band_top so the cover casts a shadow onto its own lower
        edge, then ramps up to the full info_wash and holds it to the bottom —
        giving a smooth transition into the info bar instead of a hard line.
        Cached per (band_top, info_wash)."""
        maxa = self.cfg.info_wash
        key = (band_top, maxa, self.cw, self.ch)
        if getattr(self, "_wash_key", None) == key:
            return self._wash_surf, self._wash_y

        pygame = self.pygame
        shadow_up = int(self.ch * 0.07)          # cast a shadow up onto the cover
        trans = int(self.ch * 0.14)              # ramp length below band_top
        start_y = max(0, band_top - shadow_up)
        full_y = band_top + trans
        span = max(1, full_y - start_y)
        h = self.ch - start_y
        surf = pygame.Surface((self.cw, h), pygame.SRCALPHA)
        for yy in range(h):
            if yy >= span:
                a = maxa
            else:
                a = int(maxa * smoothstep(yy / span))
            pygame.draw.line(surf, (0, 0, 0, a), (0, yy), (self.cw, yy))

        self._wash_key, self._wash_surf, self._wash_y = key, surf, start_y
        return surf, start_y

    # -- drawing ----------------------------------------------------------- #

    def render(self, cover, np: NowPlaying, text_alpha: int = 255):
        self.wake()
        self._status_key = None      # a cover is up; force status redraw next time
        screen = self.screen
        # Info band starts where the (full-width) cover ends; shared by the
        # background wash and the text overlay so they line up.
        self._cur_band_top = self._band_top(cover)
        screen.blit(self._background(cover), (0, 0))

        if cover is not None:
            scaled, pos = self._scaled_cover(cover)
            # The blurred/saturated backdrop (lms-material style) shows through
            # around a non-square cover — no edge-extend fill. Draw the sharp,
            # aspect-correct cover centred in the square zone on top.
            screen.blit(scaled, pos)
            # Soft contact shadow cast down from the cover's bottom edge.
            if self._info_h > 0:
                ss, sy = self._cover_shadow(self._cur_band_top)
                screen.blit(ss, (0, sy))

        # Soft wash/shadow over the info band (drawn after the cover so the
        # shadow falls onto the cover's lower edge). Persists with the text.
        if self._info_h > 0 and self.cfg.info_wash > 0:
            ws, wy = self._wash_overlay(self._cur_band_top)
            screen.blit(ws, (0, wy))

        if text_alpha > 0:
            ov = self._text_overlay(np)
            if ov is not None:
                if text_alpha < 255:                     # fading: scale alpha
                    ov = ov.copy()
                    ov.fill((255, 255, 255, text_alpha),
                            special_flags=self.pygame.BLEND_RGBA_MULT)
                screen.blit(ov, (0, 0))
        if np.mode == "pause":
            self._draw_pause(screen)
        self.present()

    def _draw_pause(self, screen):
        """Overlay a pause indicator on the cover so a paused track is obvious.
        A soft dark disc backs the bars so they never camouflage against a bright
        or busy cover (e.g. a white logo)."""
        pygame = self.pygame
        veil = pygame.Surface((self.cw, self.ch), pygame.SRCALPHA)
        veil.fill((0, 0, 0, 130))                        # dim the art clearly
        screen.blit(veil, (0, 0))
        cx = self.cw // 2
        cy = (self.ch - self._info_h) // 2               # middle of the cover area
        bw = max(14, self.cw // 22)                      # bar width
        bh = max(48, self.cw // 6)                       # bar height
        gap = bw
        r = max(3, bw // 3)
        # Dark backing disc for contrast against bright/busy art.
        disc_r = int(bh * 0.95)
        disc = pygame.Surface((disc_r * 2, disc_r * 2), pygame.SRCALPHA)
        pygame.draw.circle(disc, (0, 0, 0, 115), (disc_r, disc_r), disc_r)
        screen.blit(disc, (cx - disc_r, cy - disc_r))
        col = (228, 228, 234)
        pygame.draw.rect(screen, col, (cx - gap // 2 - bw, cy - bh // 2, bw, bh),
                         border_radius=r)
        pygame.draw.rect(screen, col, (cx + gap // 2, cy - bh // 2, bw, bh),
                         border_radius=r)

    def present(self):
        self._blit_to_fb()

    def _orient(self, arr):
        """Rotate the row-major (ch,cw,3) canvas image to the physical (h,w,3)
        framebuffer orientation. The fb is fixed landscape; for a 90/270 mount we
        composed portrait and rotate here at scanout."""
        k = {0: 0, 90: 3, 180: 2, 270: 1}[self.rot]
        return self.np.rot90(arr, k) if k else arr

    def _blit_to_fb(self):
        np = self.np
        arr = np.transpose(self.pygame.surfarray.array3d(self.screen), (1, 0, 2))  # (ch,cw,3) RGB
        arr = self._orient(arr)                         # rotate to (h,w,3)
        out = np.empty((self.h, self.w, 4), np.uint8)   # 32-bit BGRX (firmware fb order)
        out[..., 0] = arr[..., 2]                       # B
        out[..., 1] = arr[..., 1]                       # G
        out[..., 2] = arr[..., 0]                       # R
        out[..., 3] = 255                               # X / alpha (ignored)
        buf = out.tobytes()
        row = self.w * 4
        if self.stride == row:
            self._fb[:len(buf)] = buf
        else:
            for y in range(self.h):
                off = y * self.stride
                self._fb[off:off + row] = buf[y * row:(y + 1) * row]

    def _wrap(self, font, text, max_w):
        """Greedy word-wrap `text` to fit `max_w` px, never splitting a word.
        A single word wider than max_w stays on its own line (not cut)."""
        words = text.split()
        if not words:
            return [text]
        lines, cur = [], words[0]
        for w in words[1:]:
            trial = cur + " " + w
            if font.size(trial)[0] <= max_w:
                cur = trial
            else:
                lines.append(cur)
                cur = w
        lines.append(cur)
        return lines

    def _text_overlay(self, np: NowPlaying):
        """Build (and cache per track) a full-screen SRCALPHA overlay holding
        the scrim + artist/title text, so render() can fade it in/out by
        scaling the overlay's alpha."""
        key = (np.title, np.artist, np.album, self.cfg.show_album)
        if getattr(self, "_ov_key", None) == key:
            return self._ov_surf

        pygame = self.pygame
        lines = []
        if np.title:
            lines.append((self.font_title, np.title, (255, 255, 255)))
        if np.artist:
            lines.append((self.font_sub, np.artist, (210, 210, 210)))
        if self.cfg.show_album and np.album:
            lines.append((self.font_sub, np.album, (170, 170, 170)))

        if not lines:
            self._ov_key, self._ov_surf = key, None
            return None

        pad = max(16, self.ch // 48)
        side = max(20, self.cw // 20)                 # keep text off the edges
        max_w = self.cw - 2 * side
        rendered = []
        for f, t, c in lines:
            for sub in self._wrap(f, t, max_w):
                rendered.append((f.render(sub, True, c),
                                 f.render(sub, True, (0, 0, 0))))
        total_h = sum(fg.get_height() for fg, _ in rendered) + pad * (len(rendered) - 1)

        ov = pygame.Surface((self.cw, self.ch), pygame.SRCALPHA)
        if self._info_h > 0:
            # Stacked layout: text centered within the band below the cover (the
            # band itself is washed out in _background, so no scrim needed here).
            band_top = getattr(self, "_cur_band_top", self.ch - self._info_h)
            y = band_top + (self.ch - band_top - total_h) // 2
        else:
            # Centered (landscape) layout: gradient scrim + text along the bottom.
            scrim_h = total_h + pad * 3
            scrim = pygame.Surface((self.cw, scrim_h), pygame.SRCALPHA)
            for sy in range(scrim_h):
                a = int(170 * (sy / scrim_h))
                pygame.draw.line(scrim, (0, 0, 0, a), (0, sy), (self.cw, sy))
            ov.blit(scrim, (0, self.ch - scrim_h))
            y = self.ch - total_h - pad

        for fg, shadow in rendered:
            x = (self.cw - fg.get_width()) // 2
            ov.blit(shadow, (x + 2, y + 2))
            ov.blit(fg, (x, y))
            y += fg.get_height() + pad

        self._ov_key, self._ov_surf = key, ov
        return ov

    # -- blanking ---------------------------------------------------------- #

    def blank(self):
        # We own the CRTC, so a black fill is the blank (the panel stays powered).
        if self.blanked:
            return
        self.blanked = True
        self.screen.fill((0, 0, 0))
        self.present()

    def wake(self):
        self.blanked = False

    def quit(self):
        try:
            self._fb.close()
            os.close(self._fbfd)
        except (OSError, AttributeError):
            pass
        self.pygame.quit()


# --------------------------------------------------------------------------- #
# HDMI power
# --------------------------------------------------------------------------- #

class HdmiPower:
    """Physically powers the HDMI output off/on via a swappable shell command.

    At startup we READ the real power state (hdmi_query_cmd) rather than assume:
    at a real boot the firmware has HDMI on, so we record 'on' and issue no
    command (no redundant re-lock of the slow scaler — splash continuity is
    preserved); but after a *service restart* while the panel was resting, the
    hardware is still off from the prior run, and querying lets us wake it
    instead of wrongly believing it is on. If the query is unset/unavailable we
    default to 'on' (the firmware's boot state). `set()` is idempotent and cheap,
    so the main loop can call it every frame. Failures are logged and non-fatal.
    """

    def __init__(self, cfg: Config):
        self.on_cmd = cfg.hdmi_on_cmd
        self.off_cmd = cfg.hdmi_off_cmd
        self.on = self._query(cfg.hdmi_query_cmd)

    def _query(self, cmd: str) -> bool:
        """Best-effort read of the current HDMI power; True if on/unknown."""
        if not cmd:
            return True
        try:
            out = subprocess.run(shlex.split(cmd), timeout=5,
                                 capture_output=True, text=True)
            s = (out.stdout or "").strip()
            if s and s[-1] in "01":           # e.g. "display_power=0"
                state = s[-1] == "1"
                print(f"HDMI initial state: {'on' if state else 'off'}", flush=True)
                return state
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"[warn] HDMI query failed: {exc}", flush=True)
        return True

    def set(self, want_on: bool):
        if want_on == self.on:
            return                # debounce: nothing to do (cheap per-frame call)
        cmd = self.on_cmd if want_on else self.off_cmd
        self.on = want_on         # flip first so a failed cmd isn't retried in a spin
        if not cmd:
            return                # empty cmd = user disabled this direction
        try:
            subprocess.run(shlex.split(cmd), timeout=5,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"[warn] HDMI {'on' if want_on else 'off'} command failed: "
                  f"{exc}", flush=True)


# --------------------------------------------------------------------------- #
# Main loop
# --------------------------------------------------------------------------- #

# Player states (computed once per poll; precedence handled in classify()).
S_UNREACHABLE = "unreachable"        # LMS not reachable — keep panel up, show status
S_NO_PLAYER = "no_player"            # reachable, but no player to follow
S_POWER_OFF = "power_off"            # player powered off (LMS power=0)
S_PLAYING = "playing"               # play + cover ready
S_PLAYING_NO_COVER = "playing_no_cover"  # play, but cover not fetched yet
S_PAUSED = "paused"                 # pause, kept lit
S_PAUSED_BLANK = "paused_blank"     # pause, treated as idle (blank_on_pause)
S_STOPPED = "stopped"               # stop

# States that mean "audio is engaged" (drives cover fetch / overlay timing).
_ENGAGED = (S_PLAYING, S_PLAYING_NO_COVER, S_PAUSED)
# States whose idle_blank_seconds timer, once elapsed, powers the HDMI off.
# NO_PLAYER counts as idle: nobody is using the system, so rest the panel too.
_IDLE_OFF = (S_STOPPED, S_PAUSED_BLANK, S_NO_PLAYER)

# Status-screen text per state (only shown when there's no cover to render).
_STATUS_TEXT = {
    S_UNREACHABLE: "connecting…",
    S_NO_PLAYER: "waiting for player…",
    S_POWER_OFF: "off",
    S_PLAYING: "loading…",
    S_PLAYING_NO_COVER: "loading…",
    S_PAUSED: "paused",
    S_PAUSED_BLANK: "paused",
    S_STOPPED: "stopped",
}


def classify(cfg: Config, np: NowPlaying, power: int, cover_ready: bool) -> str:
    """Map a successful poll to a single player state (first match wins).

    `power` is the top-level LMS status field (0/1); it takes precedence over
    `mode` so a player reporting power=0 with a stale 'play' still reads as off.
    """
    if power == 0:
        return S_POWER_OFF
    if np.mode == "play":
        return S_PLAYING if cover_ready else S_PLAYING_NO_COVER
    if np.mode == "pause":
        return S_PAUSED_BLANK if cfg.blank_on_pause else S_PAUSED
    return S_STOPPED


def _text_alpha(now, text_until, show_seconds, fade):
    """Overlay opacity 0-255: full until the last `fade` seconds, then ramps
    down to 0 at text_until. 0 = always-on (text_show_seconds <= 0)."""
    if show_seconds <= 0:
        return 255
    rem = text_until - now
    if rem <= 0:
        return 0
    if rem >= fade or fade <= 0:
        return 255
    return max(0, min(255, int(255 * rem / fade)))


def _next_wake(cfg, now, heartbeat_at, idle_since, power_off_since,
               unreachable_since, text_until, playing, show_secs, fade, frame):
    """How long to block in select() before the loop must run again even with no
    event: the soonest of the heartbeat re-sweep, any armed rest/grace timer's
    exact deadline (so HDMI rests on time without polling), and the fade frame
    tick while text is animating. Clamped to a small floor."""
    wake = heartbeat_at - now
    if idle_since is not None:
        wake = min(wake, (idle_since + cfg.idle_blank_seconds) - now)
    if power_off_since is not None:
        wake = min(wake, (power_off_since + cfg.hdmi_off_grace) - now)
    if unreachable_since is not None:
        deadline = max(cfg.idle_blank_seconds, cfg.unreachable_grace)
        wake = min(wake, (unreachable_since + deadline) - now)
    if playing and show_secs > 0:
        rem = text_until - now
        if rem > fade:
            wake = min(wake, rem - fade)      # wake when the fade begins
        elif rem > 0:
            wake = min(wake, frame)           # animate the fade
    return max(0.02, wake)


def run(cfg: Config):
    # SDL only composes an offscreen surface; we copy it to /dev/fb0 ourselves.
    os.environ["SDL_VIDEODRIVER"] = "dummy"
    os.environ.setdefault("SDL_AUDIODRIVER", "dummy")  # we never play audio
    os.environ["PYGAME_HIDE_SUPPORT_PROMPT"] = "1"     # no import banner

    # systemd sends SIGTERM on stop/restart; reuse the KeyboardInterrupt path so
    # the finally-block cleanup runs and we exit promptly (no 90s kill timeout).
    def _on_term(_signum, _frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _on_term)

    client = LMSClient(cfg)
    display = Display(cfg)     # shows the "loading…" status immediately
    hdmi = HdmiPower(cfg)      # firmware already has HDMI up; tracks on/off
    listener = EventListener(cfg)
    listener.connect()         # best-effort; falls back to polling if it fails

    # Multi-player: the configured players (priority order) are all polled each
    # tick; exactly one drives the display. `specs` empty = legacy single-player.
    specs = cfg.players or ([cfg.player] if cfg.player else [])
    players_resolved = []     # currently-connected configured MACs, priority order
    last_active = None        # MAC most recently seen playing (sticky idle target)
    prev_selected = None      # to detect a display switch and reset transient state
    cover_surface = None
    cover_cache = {}          # cover_key -> decoded surface
    last_key = None
    last_track = None         # (title, artist, album, cover_key) — detects track changes
    last_state_id = None      # render identity, to avoid needless redraws
    idle_since = None         # entered stop / paused-blank at this monotonic time
    power_off_since = None    # player reported power=0 at this monotonic time
    unreachable_since = None  # LMS first went unreachable at this monotonic time
    state = S_UNREACHABLE     # current player state, persists between polls
    text_until = 0.0          # overlay fully gone at this monotonic time
    was_playing = False
    np = NowPlaying()
    playing = False
    dirty = True              # force the first sweep; set by pushed events
    heartbeat_at = 0.0        # next safety re-sweep deadline
    FRAME = 1.0 / 30          # animation tick during the text fade
    fade = max(0.0, cfg.text_fade_seconds)
    # In the stacked layout the bottom band is a dedicated info area, so keep the
    # text on permanently (0 = always-on); only the centered layout fades it out.
    show_secs = 0.0 if cfg.info_height > 0 else cfg.text_show_seconds
    status_msg = "loading…"   # status-screen text shown whenever no cover is up

    print("Lyrion Cover Display running. Ctrl-C to quit.", flush=True)
    try:
        while True:
            now = time.monotonic()

            # --- sweep LMS when an event marked us dirty or the heartbeat is due
            # (the event socket pushes changes; this is no longer a fixed poll) ---
            if dirty or now >= heartbeat_at:
                dirty = False
                heartbeat_at = now + (cfg.event_heartbeat if listener.connected
                                      else cfg.poll_interval)
                try:
                    # Resolve + poll EVERY configured player each tick (cheap
                    # metadata only). Re-resolving handles players connecting /
                    # disconnecting after boot. Network failure -> except below.
                    players_resolved = client.resolve_players(specs)
                    if not players_resolved:
                        raise RuntimeError("No configured player is connected.")
                    polls = {}            # mac -> (NowPlaying, power)
                    playing_macs = []     # connected + playing, in priority order
                    for mac in players_resolved:
                        st = client.status(mac)
                        npx = NowPlaying.parse(cfg, st)
                        try:
                            pw = int(st.get("power", 1))
                        except (TypeError, ValueError):
                            pw = 1
                        polls[mac] = (npx, pw)
                        if pw == 1 and npx.mode == "play":   # only play = active
                            playing_macs.append(mac)

                    # --- pick the one player to display ---
                    # Strict priority among players currently playing; otherwise
                    # stick with the last player that played (sticky idle), or the
                    # highest-priority player before anything has played.
                    if playing_macs:
                        selected = playing_macs[0]
                        last_active = selected
                    elif last_active in players_resolved:
                        selected = last_active
                    else:
                        selected = players_resolved[0]
                        last_active = selected

                    # A switch must not carry the previous player's timers / cover
                    # over: reset the transient render + idle state cleanly.
                    if selected != prev_selected:
                        if prev_selected is None:
                            print(f"Players {players_resolved}; showing {selected}",
                                  flush=True)
                        else:
                            print(f"Display switched to player: {selected}",
                                  flush=True)
                        prev_selected = selected
                        idle_since = power_off_since = unreachable_since = None
                        was_playing = False
                        cover_surface = None
                        last_key = None
                        last_track = None
                        last_state_id = None
                        text_until = 0.0

                    np, power = polls[selected]
                    # "Engaged" = audio active (play, or pause kept lit): drives the
                    # cover fetch and the overlay-fade timer, as before.
                    engaged = power == 1 and (np.mode == "play" or (
                        np.mode == "pause" and not cfg.blank_on_pause))

                    if engaged:
                        track = (np.title, np.artist, np.album, np.cover_key)
                        # New track / resume -> show overlay, then let it fade.
                        if track != last_track or not was_playing:
                            last_track = track
                            text_until = (float("inf") if show_secs <= 0
                                          else now + show_secs + fade)
                        # Fetch cover only when the art identity changes.
                        if np.cover_key and np.cover_key != last_key:
                            surf = cover_cache.get(np.cover_key)
                            if surf is None:
                                surf = _fetch_cover(client, display, np.cover_url)
                                if surf is not None:
                                    cover_cache[np.cover_key] = surf
                                    _trim_cache(cover_cache)
                            # Adopt the new art; if the fetch failed, drop to
                            # no-cover for this key so we show the status screen
                            # rather than the *previous* track's cover.
                            cover_surface = surf
                            last_key = np.cover_key
                        was_playing = True
                    else:
                        was_playing = False

                    state = classify(cfg, np, power, cover_surface is not None)
                    playing = state in (S_PLAYING, S_PAUSED)
                    status_msg = _STATUS_TEXT[state]
                except (*NET_ERRORS, ValueError, RuntimeError,
                        KeyError, TypeError, AttributeError) as exc:
                    print(f"[warn] LMS poll failed: {exc}", flush=True)
                    players_resolved = []   # re-resolve every player next tick
                    # NO_PLAYER is genuine idle: the timer section below rests the
                    # panel (HDMI off) after idle_blank_seconds, just like a stop.
                    # A transient UNREACHABLE blip instead holds the current power
                    # state (see want_on) so a network hiccup never wakes a resting
                    # panel nor needlessly re-locks the slow scaler. Fall through to
                    # the unified timer / rest / render section.
                    state = (S_NO_PLAYER if isinstance(exc, RuntimeError)
                             else S_UNREACHABLE)
                    playing = False
                    status_msg = _STATUS_TEXT[state]
                    # Back off the retry sweep (events can't be trusted while the
                    # HTTP side is failing); don't re-sweep faster than ~2s.
                    heartbeat_at = now + max(2.0, cfg.poll_interval)

            # --- timers: arm on entry to an off-able state, clear on leaving --- #
            now = time.monotonic()
            if state in _IDLE_OFF:
                if idle_since is None:
                    idle_since = now
                    last_state_id = None
            else:
                idle_since = None
            if state == S_POWER_OFF:
                if power_off_since is None:
                    power_off_since = now
                    last_state_id = None
            else:
                power_off_since = None
            if state == S_UNREACHABLE:
                if unreachable_since is None:
                    unreachable_since = now
                    last_state_id = None
            else:
                unreachable_since = None

            # --- decide whether the panel should rest (dark / HDMI off) --- #
            resting = (
                (idle_since is not None
                 and now - idle_since >= cfg.idle_blank_seconds)
                or (power_off_since is not None
                    and now - power_off_since >= cfg.hdmi_off_grace))
            if state in _ENGAGED:
                # Active content (playing / paused): always on — wakes the panel.
                want_on = True
            elif state == S_UNREACHABLE:
                # A server blip must not wake a resting panel nor blank a live one:
                # hold whatever power state we're already in until LMS is back. But
                # a *sustained* outage (past idle_blank_seconds) rests the panel like
                # idle — recovery to a playing state re-wakes it.
                long_out = (unreachable_since is not None
                            and now - unreachable_since >= cfg.idle_blank_seconds)
                want_on = hdmi.on and not (long_out and cfg.power_blank_enabled)
            else:
                # Rest-able (stopped / paused-blank / no-player / power-off): hold
                # an already-lit panel (e.g. the cover after a stop) until the
                # timer elapses, then power off. Never wake an already-off panel
                # just to wait out a grace/idle timer — that would flap the slow
                # scaler (e.g. powering the player off while the panel already
                # rests must keep it dark, not relight it for the grace window).
                want_on = hdmi.on and not (resting and cfg.power_blank_enabled)

            # --- render + drive HDMI power (guarded: a transient framebuffer /
            # pygame / vcgencmd error must never kill the 24/7 service; we log it
            # and keep looping, leaving the last good frame up) --- #
            try:
                if want_on and not hdmi.on:
                    # Wake: lay a coherent splash frame into the framebuffer, then
                    # re-assert the signal. The scaler shows nothing for ~8-9s while
                    # it cold-locks; the render block below keeps writing full frames
                    # (the resumed cover, or a status screen) so whatever the scaler
                    # finally locks onto is complete, never a torn/stale frame.
                    display.status_screen("loading…")
                    last_state_id = None
                    hdmi.set(True)

                if not want_on:
                    # Rest the panel: black-fill first (fail-safe if display_power is
                    # a no-op on this firmware), then physically drop the signal.
                    display.blank()
                    hdmi.set(False)
                elif resting:
                    # Resting, but HDMI kept on (feature disabled): legacy black-fill.
                    display.blank()
                elif state in (S_PLAYING, S_PAUSED) and cover_surface is not None:
                    alpha = _text_alpha(now, text_until, show_secs, fade)
                    state_id = (state, last_key, alpha, last_track)
                    if state_id != last_state_id or display.blanked:
                        display.render(cover_surface, np, alpha)
                        last_state_id = state_id
                elif state == S_UNREACHABLE:
                    # Server/wifi blip: hold whatever is on screen (usually the last
                    # cover) so a brief outage is invisible. Only after the grace —
                    # or if we never had a cover (cold boot) — do we show
                    # "connecting…". Forcing last_state_id=None makes the cover
                    # repaint cleanly once LMS returns.
                    if (cover_surface is None or unreachable_since is None
                            or now - unreachable_since >= cfg.unreachable_grace):
                        display.status_screen(status_msg)
                        last_state_id = None
                    # else: hold the last frame (no draw)
                else:
                    # Status screen: connecting / waiting for player / stopped /
                    # paused / off (incl. the power-off grace window), and "playing
                    # but no cover fetched yet". Shows the splash + status text
                    # rather than a bare, text-less cover before the signal drops.
                    display.status_screen(status_msg)
            except Exception as exc:  # noqa: BLE001  (render/fb/vcgencmd hiccup)
                print(f"[error] render/power step failed: {exc}", flush=True)
                last_state_id = None      # force a clean redraw next iteration

            # --- wait for a pushed event, or until the next required deadline ---
            now = time.monotonic()
            timeout = _next_wake(cfg, now, heartbeat_at, idle_since,
                                 power_off_since, unreachable_since, text_until,
                                 playing, show_secs, fade, FRAME)
            if listener.connected:
                try:
                    readable, _, _ = select.select([listener.fileno()], [], [],
                                                   timeout)
                except (OSError, ValueError):
                    listener.mark_down()
                    readable = []
                if readable:
                    lines, alive = listener.drain()
                    if lines:
                        dirty = True             # a real change pushed — re-sweep
                    if not alive:
                        print("[warn] event socket dropped; polling until "
                              "reconnect.", flush=True)
            else:
                # Socket down: sleep, then attempt a backed-off reconnect. The
                # heartbeat (at poll_interval while down) keeps the display live.
                time.sleep(timeout)
                if listener.try_reconnect(time.monotonic()):
                    dirty = True                 # resync immediately on reconnect
    except KeyboardInterrupt:
        pass
    finally:
        listener.close()
        # Leave the panel powered so the next boot splash shows immediately.
        if not hdmi.on and cfg.hdmi_on_cmd:
            hdmi.set(True)
        display.quit()


def _fetch_cover(client: LMSClient, display: Display, url: str):
    try:
        return display.decode_cover(client.get_bytes(url))
    except Exception as exc:  # noqa: BLE001  (network or decode failure)
        print(f"[warn] cover fetch/decode failed ({url}): {exc}", flush=True)
        return None


def _trim_cache(cache: dict, limit: int = 8):
    while len(cache) > limit:
        cache.pop(next(iter(cache)))


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def cmd_list_players(cfg: Config):
    client = LMSClient(cfg)
    players = client.list_players()
    if not players:
        print("No players connected to LMS.")
        return
    print(f"Players on {cfg.base_url}:")
    for p in players:
        connected = "connected" if p.get("connected") else "offline"
        print(f"  {p.get('playerid')}  {p.get('name')}  [{connected}]")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Lyrion Cover Display kiosk")
    ap.add_argument("--config", default=_default_config_path(),
                    help="path to config.ini (default: alongside this script)")
    ap.add_argument("--list-players", action="store_true",
                    help="list connected players (and their MACs) and exit")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    if args.list_players:
        cmd_list_players(cfg)
        return
    # Any unexpected escape from the loop becomes a logged non-zero exit so
    # systemd restarts cleanly, rather than a bare traceback.
    try:
        run(cfg)
    except KeyboardInterrupt:
        pass
    except Exception as exc:  # noqa: BLE001
        import traceback
        print(f"[fatal] {exc}", flush=True)
        traceback.print_exc()
        sys.exit(1)


def _default_config_path():
    here = os.path.dirname(os.path.abspath(__file__))
    local = os.path.join(here, "config.ini")
    return local if os.path.exists(local) else None


if __name__ == "__main__":
    main()
