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
    "cover_px": "1200",      # requested cover size from LMS (server-side resize)
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
    # Internet radio: look up a high-quality cover for the current song via the
    # covers.musichoarders.xyz search aggregator; only EXACT artist+album
    # matches are shown, and a found cover is perceptually compared against the
    # station's own per-song art (when it has any) before being adopted.
    "radio_cover_search": "true",
    "radio_cover_country": "de",   # store country for the search (NOTE: "fi" is rejected upstream)
    # Preference order = artwork QUALITY, not just resolution: everything is
    # resized to cover_px by the LMS imageproxy anyway, so the order decides
    # which ORIGINAL feeds that resize. Apple serves the cleanest originals,
    # Tidal hosts the label's own upload (origin.jpg), Amazon full-size
    # scans, Spotify re-encodes its 2000px class. (bugs excluded: variable
    # quality and its CDN regularly times out through the imageproxy.)
    "radio_cover_sources": "applemusic, tidal, amazonmusic, spotify",
    "radio_cover_title_fallback": "false",  # true = when the stream has no album tag, search the song title as an album name (usually finds the single)
    "radio_cover_timeout": "8.0",  # wall-clock deadline for one search (seconds)
    "radio_cover_match_threshold": "16",  # max dHash distance (0-64) vs station art; higher = laxer
    "radio_cover_loose_match": "true",  # also accept punctuation variants + decorated editions (Deluxe/Single/Remastered…) — ONLY when the station's own per-song art visually confirms them
    "upgrade_fade_seconds": "0.4",  # crossfade when a sharper cover replaces art already on screen (hi-res upgrade, radio cover); 0 = hard cut
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
    radio_cover_search: bool
    radio_cover_country: str
    radio_cover_sources: list[str]
    radio_cover_title_fallback: bool
    radio_cover_timeout: float
    radio_cover_match_threshold: int
    radio_cover_loose_match: bool
    upgrade_fade_seconds: float

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
        country = s.get("radio_cover_country", "de").strip().lower()
        if not (len(country) == 2 and country.isascii() and country.isalpha()):
            print(f"[warn] radio_cover_country '{country}' invalid; using 'de'.",
                  flush=True)
            country = "de"
        radio_sources = [p for p in s.get("radio_cover_sources", "")
                         .replace(",", " ").split() if p]
        radio_search = s.getboolean("radio_cover_search")
        if radio_search and not radio_sources:
            print("[warn] radio_cover_sources is empty; radio cover search "
                  "disabled.", flush=True)
            radio_search = False
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
            radio_cover_search=radio_search,
            radio_cover_country=country,
            radio_cover_sources=radio_sources,
            radio_cover_title_fallback=s.getboolean("radio_cover_title_fallback"),
            radio_cover_timeout=max(1.0, s.getfloat("radio_cover_timeout")),
            radio_cover_match_threshold=max(
                0, min(64, s.getint("radio_cover_match_threshold"))),
            radio_cover_loose_match=s.getboolean("radio_cover_loose_match"),
            upgrade_fade_seconds=max(
                0.0, min(2.0, s.getfloat("upgrade_fade_seconds"))),
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
        # Two playlist entries: [0] = the current track, [1] = the upcoming
        # track (used to prefetch its cover so song changes swap instantly).
        return self.request(player, ["status", "-", 2, "tags:aclKxN"])


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

# Spotify CDN image-id size prefixes: the first 16 hex chars of an i.scdn.co
# image id select a size class of the SAME image. Stock Spotty emits the
# 640px class; swapping the prefix yields the 2000px ("original") variant —
# derived HERE, client-side, so the LMS UI/mosaics never pay the big fetches
# (a server-side hi-res plugin fork proved too slow for everything else).
# Anchored to i.scdn.co album-art ids inside the percent-encoded imageproxy
# URL, mirroring that fork's own anchoring — playlist mosaics, artist and
# show images are never touched.
_SCDN_HIRES = "ab67616d000082c1"   # 2000px ("original") class
_SCDN_FAST = "ab67616d0000b273"    # 640px class
_SCDN_MARK_HI = "i.scdn.co%2Fimage%2F" + _SCDN_HIRES
_SCDN_MARK_LO = "i.scdn.co%2Fimage%2F" + _SCDN_FAST


def _art_url(cfg: Config, track: dict):
    """(cover_key, cover_url, hires_url) for a status playlist_loop entry, or
    ("", "", "") when the track carries no usable art identity. hires_url is
    non-empty only for Spotify album art: cover_url is the fast 640px variant
    to paint immediately, and hires_url the slow 2000px fetch to upgrade to
    (prefetch grabs hires directly). Both emission directions are handled —
    stock Spotty's 640 ids get the hi-res variant derived by prefix swap,
    and hi-res-fork 2000px ids get the fast variant derived the same way.

    Prefer artwork_url when present. Remote sources (internet radio, some
    streams) set a synthetic/negative coverid that does NOT resolve via
    /music/<id>/cover, but DO provide a real artwork_url (often an
    /imageproxy/... link). Local tracks have no artwork_url, so they fall
    to coverid.

    We never fetch remote art directly: a bare /imageproxy/<enc>/image.png
    301-redirects to the remote (often https), and this Pi has no RTC — at
    boot the clock is stale until NTP, so the CDN's TLS cert reads "not
    yet valid" and the cover fails. Adding a size spec (image_NxN_o.png)
    makes LMS fetch + resize server-side and return the bytes itself, so
    the Pi only ever does plain LAN HTTP to LMS, clock-independent. The .png
    spec is lossless (a .jpg spec would re-encode ~37dB, second-generation
    loss); its slower decode is hidden by the next-track prefetch. Note the
    _o spec never upscales: LMS returns min(requested, native) size, and the
    renderer smoothscales the rest — one resample, best quality."""
    coverid = track.get("coverid")
    artwork_url = track.get("artwork_url")
    px = cfg.cover_px
    if artwork_url:
        # Normalise the key across size classes so 82c1/b273 emissions of the
        # same image share one cache identity (fork toggles, stale metadata).
        key = f"url:{artwork_url.replace(_SCDN_HIRES, _SCDN_FAST, 1)}"
        if artwork_url.startswith(cfg.base_url):
            artwork_url = artwork_url[len(cfg.base_url):]  # absolute-to-LMS
        if artwork_url.startswith("http"):
            # External absolute URL: route through the LMS imageproxy.
            url = (f"{cfg.base_url}/imageproxy/"
                   f"{quote(artwork_url, safe='')}/image_{px}x{px}_o.png")
        else:
            rel = artwork_url.lstrip("/")
            if rel.startswith("imageproxy/") and rel.endswith("/image.png"):
                rel = rel[:-len("image.png")] + f"image_{px}x{px}_o.png"
            url = f"{cfg.base_url}/{rel}"
        if _SCDN_MARK_HI in url:
            return key, url.replace(_SCDN_HIRES, _SCDN_FAST, 1), url
        if _SCDN_MARK_LO in url:
            return key, url, url.replace(_SCDN_FAST, _SCDN_HIRES, 1)
        return key, url, ""
    if coverid:
        # The _o suffix = keep the artwork's native aspect, max dimension px,
        # no square pad/crop (plain cover_NxN.jpg squares it). We scale and
        # blur-fill ourselves, so we want the original aspect ratio.
        return (f"cid:{coverid}",
                f"{cfg.base_url}/music/{quote(str(coverid))}/cover_{px}x{px}_o.png",
                "")
    return "", "", ""


@dataclass
class NowPlaying:
    mode: str = "stop"           # play | pause | stop
    title: str = ""
    artist: str = ""
    album: str = ""
    cover_key: str = ""          # identity used to detect art changes
    cover_url: str = ""          # absolute URL to fetch the cover from
    cover_hires_url: str = ""    # slow full-quality variant ("" = cover_url is final)
    next_cover_key: str = ""     # upcoming track's art identity (prefetch)
    next_cover_url: str = ""     # upcoming track's art URL (prefetch)
    next_cover_hires_url: str = ""
    remote: bool = False         # current track is a stream (radio, Spotify, …)

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
        np.remote = bool(status.get("remote") or track.get("remote"))

        np.cover_key, np.cover_url, np.cover_hires_url = _art_url(cfg, track)
        if not np.cover_key:
            # Last resort: the "current cover" shortcut for this player.
            # cover_hires_url stays "" — this form is final, never upgraded.
            px = cfg.cover_px
            pid = status.get("playerid", "")
            np.cover_key = "current"
            np.cover_url = (
                f"{cfg.base_url}/music/current/cover_{px}x{px}_o.png?player={quote(pid)}"
            )
        # The sweep asks for two playlist entries; entry [1] is the upcoming
        # track, whose art we prefetch so the swap at the song change is
        # instant. Only real identities are prefetchable (no "current" form).
        if len(loop) > 1:
            (np.next_cover_key, np.next_cover_url,
             np.next_cover_hires_url) = _art_url(cfg, loop[1])
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
        font_candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            os.path.join(os.path.dirname(__file__), "assets", "DejaVuSans-Bold.ttf"),
        ]
        regular_candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            os.path.join(os.path.dirname(__file__), "assets", "DejaVuSans.ttf"),
        ]
        self._font_cache = {}
        self._base = max(20, self.ch // 28)
        # Resolve the font files once; the text overlay re-instantiates them at
        # smaller sizes on the fly (see _font_at) to shrink names that would
        # otherwise overflow the info band.
        self._font_title_path = self._resolve_font(font_candidates)
        self._font_sub_path = self._resolve_font(regular_candidates)
        self.font_title = self._font_at(self._font_title_path, self._base)
        self.font_sub = self._font_at(self._font_sub_path, int(self._base * 0.7))
        self.font_status = self._font_at(self._font_sub_path, self._base)  # status
        # Per-line script fallback: DejaVu has no Ethiopic/CJK/etc glyphs
        # (radio metadata arrives in the artist's own script — observed live:
        # "ሙላቱ አስታጥቄ" rendered as boxes). GNU FreeSerif covers far more
        # scripts and ships on the Pi already; a line whose characters the
        # primary font lacks is rendered wholly with the fallback instead.
        self._font_fallback = {
            self._font_title_path:
                self._resolve_font(
                    ["/usr/share/fonts/truetype/freefont/FreeSerifBold.ttf"]),
            self._font_sub_path:
                self._resolve_font(
                    ["/usr/share/fonts/truetype/freefont/FreeSerif.ttf"]),
        }
        self._ft_probe = {}   # font path -> pygame.freetype.Font (glyph probe)
        self._glyph_ok = {}   # (font path, char) -> bool

    def _resolve_font(self, candidates):
        for path in candidates:
            if os.path.exists(path):
                return path
        return None  # pygame's built-in fallback (Font(None, size))

    def _font_at(self, path, size):
        size = max(1, int(size))
        cached = self._font_cache.get((path, size))
        if cached is None:
            cached = self.pygame.font.Font(path, size)
            self._font_cache[(path, size)] = cached
        return cached

    def _covers(self, path, text):
        """True when the font file at `path` has a real glyph for every
        non-ASCII character of `text`. Uses pygame.freetype: its get_metrics
        returns None entries for absent glyphs, whereas pygame.font.metrics
        happily reports the .notdef box as if it were a glyph."""
        if path is None:
            return True          # pygame builtin — nothing to probe
        probe = self._ft_probe.get(path)
        if probe is None:
            import pygame.freetype
            pygame.freetype.init()
            probe = self._ft_probe[path] = pygame.freetype.Font(path, 16)
        for ch in {c for c in text if ord(c) > 127}:
            ok = self._glyph_ok.get((path, ch))
            if ok is None:
                try:
                    m = probe.get_metrics(ch)
                    ok = bool(m) and m[0] is not None
                except Exception:  # noqa: BLE001 (freetype hiccup: assume ok)
                    ok = True
                self._glyph_ok[(path, ch)] = ok
            if not ok:
                return False
        return True

    def _line_font_path(self, path, text):
        """Font file for one text line: the primary unless it is missing
        glyphs AND the fallback actually has them (else keep primary — a
        box is no worse in a font that also lacks the script)."""
        if self._covers(path, text):
            return path
        fb = self._font_fallback.get(path)
        if fb and self._covers(fb, text):
            return fb
        return path

    # -- cover decoding/scaling ------------------------------------------- #

    def decode_cover(self, data: bytes):
        pygame = self.pygame
        img = pygame.image.load(io.BytesIO(data))
        # Normalise to a plain 32-bit surface by blitting onto a fresh one.
        # (Surface.convert() needs a display surface, which we don't have in
        # framebuffer mode.) We fetch the artwork at its native aspect ratio
        # (cover_NxN_o.png), so it is shown whole, scaled to fit a 1:1 square zone;
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
        (Rec.709 luma coefficients 0.213 / 0.715 / 0.072). Algebraically each
        channel of that matrix is `luma*(1-s) + channel*s`, i.e. a blend past
        the Rec.709 grayscale image — which PIL's Image.blend computes at C
        speed, extrapolating and clipping for s > 1, at full resolution.
        (PIL replaced numpy here: dropping numpy cuts ~4s of cold-boot import
        on the Pi's SD card; PIL imports in well under a second.)"""
        from PIL import Image
        pygame = self.pygame
        w, h = surf.get_size()
        img = Image.frombytes("RGB", (w, h), pygame.image.tostring(surf, "RGB"))
        gray = img.convert("L", (0.213, 0.715, 0.072, 0))
        out = Image.blend(Image.merge("RGB", (gray, gray, gray)), img, s)
        return pygame.image.fromstring(out.tobytes(), (w, h), "RGB")

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
    # Backdrop = lms-material `.np-full .np-bgnd-cover`, faithfully:
    #   filter: saturate(3) blur(...)  ->  transform: scale(1.35)
    #   box-shadow: inset 100vw 100vh rgba(48,48,48,0.8)  (dark theme = uniform tint)
    # Saturate is applied FIRST, at full res (CSS filter order): saturating before
    # the blur's averaging keeps each pixel's chroma, so the averaged wash stays
    # hued. The 0.8 grey blend then mutes the saturate(3) boost back to lms's hue.
    # Recomputed only when the cover changes (cached by the cover surface).
    _BG_SAT = 4.2                               # above lms's saturate(3) for more vivid colour through the dark tint
    _BG_ZOOM = 1.35
    _BG_TINT = (48, 48, 48)                     # dark-theme --np-bgnd-full-shadow-color
    _BG_TINT_ALPHA = 204                        # 0.8 × 255 (lms's --np-bgnd-full-shadow-color)
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
        # Keyed cache (current + prefetched next). id() is safe as the key only
        # because the entry also holds the cover ref (keeps it alive, so its id
        # can't be reused while cached).
        cache = getattr(self, "_bg_cache", None)
        if cache is None:
            cache = self._bg_cache = {}
        hit = cache.get(id(cover))
        if hit is not None and hit[0] is cover:
            return hit[1]
        cw, ch = self.cw, self.ch
        base = self._crop_fill(cover, cw, ch)
        # saturate(3) FIRST, full res (preserves chroma through the blur average),
        # THEN blur = downscale to the lms-ratio thumbnail and upscale back.
        sat = self._saturate(base, self._BG_SAT)
        tw = max(6, round(self._LMS_VIEWPORT_W / self._LMS_FILTER_PX))
        th = max(6, round(tw * ch / cw))
        thumb = pygame.transform.smoothscale(sat, (tw, th))
        zw, zh = round(cw * self._BG_ZOOM), round(ch * self._BG_ZOOM)
        bg = self._blur_up(thumb, zw, zh)
        bg = bg.subsurface(((zw - cw) // 2, (zh - ch) // 2, cw, ch)).copy()  # scale(1.35)
        # Uniform 0.8 blend toward dark grey (their inset box-shadow) — mutes the
        # oversaturation to lms's hue without going fully grey.
        veil = pygame.Surface((cw, ch))
        veil.fill(self._BG_TINT)
        veil.set_alpha(self._BG_TINT_ALPHA)
        bg.blit(veil, (0, 0))
        # Cap 5: current + prefetched next + the fast->hires upgrade pair on a
        # cold skip + slack for the transient double key-change LMS emits
        # while a track loads (placeholder art first) — too few slots churn
        # out the prewarmed backdrop and the swap pays a full recompute (~1s)
        # despite the cover-cache hit.
        while len(cache) >= 5:
            cache.pop(next(iter(cache)))
        cache[id(cover)] = (cover, bg)
        return bg

    def prewarm_background(self, cover):
        """Compute (and cache) the backdrop for a not-yet-shown cover, so the
        render at the actual song change is a pure cache hit."""
        if self.cfg.background != "black":
            self._background(cover)

    def drop_background(self, cover):
        """Evict a superseded cover's cached backdrop (e.g. the fast 640px
        surface once its hi-res replacement has been painted) — the entry
        would otherwise pin the dead surface + a full-canvas backdrop."""
        cache = getattr(self, "_bg_cache", None)
        if cache:
            cache.pop(id(cover), None)

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
        self._compose(cover, np, text_alpha)
        self.present()

    def crossfade(self, cover, np: NowPlaying, text_alpha: int = 255,
                  seconds: float = 0.4):
        """Blend from whatever is on screen to a freshly composed frame with
        `cover` — used when a sharper variant replaces art already up (hi-res
        upgrade, radio cover) so the swap reads as a focus-pull, not a cut.
        Frame pacing comes from the fb blit itself (~100-150ms/frame on the
        Pi 3); the loop blocks its caller (the heavy slot) for `seconds`."""
        if seconds <= 0 or self.blanked:
            self.render(cover, np, text_alpha)
            return
        self.wake()
        old = self.screen.copy()               # frame currently displayed
        self._compose(cover, np, text_alpha)
        new = self.screen.copy()
        t0 = time.monotonic()
        for i in range(30):                    # hard cap, belt against stalls
            k = min(1.0, (time.monotonic() - t0) / seconds)
            if i == 29:
                k = 1.0   # cap hit (fast presents): force the final frame
            new.set_alpha(int(255 * k))
            self.screen.blit(old, (0, 0))
            self.screen.blit(new, (0, 0))
            self.present()
            if k >= 1.0:
                break

    def _compose(self, cover, np: NowPlaying, text_alpha: int = 255):
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

    # Canvas-to-panel rotation as a pygame angle (positive = counterclockwise).
    # rot=90 means "canvas composed portrait, panel mounted 90° cw" — the frame
    # must be rotated 90° clockwise (-90) back to the fb's fixed landscape.
    _ROT_ANGLE = {0: 0, 90: -90, 180: 180, 270: 90}

    def _blit_to_fb(self):
        pygame = self.pygame
        surf = self.screen
        angle = self._ROT_ANGLE[self.rot]
        if angle:
            surf = pygame.transform.rotate(surf, angle)   # (cw,ch) -> (w,h)
        # Persistent surface whose pixel layout IS the firmware fb's 32-bit
        # BGRX (little-endian XRGB8888): blitting into it converts at C speed,
        # and its raw buffer copies to the fb without any per-byte shuffling.
        # (pygame 2.1.2 has no BGR* tostring format, hence this route; it also
        # replaced the old numpy pack — dropping numpy cuts ~4s of cold boot.)
        fbs = getattr(self, "_fbsurf", None)
        if fbs is None:
            fbs = self._fbsurf = pygame.Surface(
                (self.w, self.h), 0, 32, (0xFF0000, 0xFF00, 0xFF, 0))
        fbs.blit(surf, (0, 0))
        buf = bytes(fbs.get_view("0"))
        pitch = fbs.get_pitch()
        row = self.w * 4
        if self.stride == pitch:
            self._fb[:len(buf)] = buf
        else:
            for y in range(self.h):
                off = y * self.stride
                self._fb[off:off + row] = buf[y * pitch:y * pitch + row]

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

    def _ellipsize(self, font, text, max_w):
        """Trim `text` from the end and append … until it fits `max_w` px.
        Last resort for a single token too wide even at the smallest font."""
        if font.size(text)[0] <= max_w:
            return text
        ell = "…"
        s = text
        while s and font.size(s + ell)[0] > max_w:
            s = s[:-1]
        return (s + ell) if s else ell

    def _fit_lines(self, np: NowPlaying, max_w, max_h):
        """Pick the largest font scale at which the title/artist/album text wraps
        within `max_w` and the whole block fits `max_h`. Shrinks toward a floor;
        if it still won't fit, ellipsizes lines (and drops any past `max_h`) so
        the result always fits. Returns (rendered, pad): rendered is a list of
        (fg_surface, shadow_surface) pairs, pad is the inter-line gap in px."""
        # (path, base_size, text, colour) for each line that is present.
        specs = []
        if np.title:
            specs.append((self._line_font_path(self._font_title_path, np.title),
                          self._base, np.title, (255, 255, 255)))
        if np.artist:
            specs.append((self._line_font_path(self._font_sub_path, np.artist),
                          int(self._base * 0.7), np.artist, (210, 210, 210)))
        if self.cfg.show_album and np.album:
            specs.append((self._line_font_path(self._font_sub_path, np.album),
                          int(self._base * 0.7), np.album, (170, 170, 170)))
        if not specs:
            return [], 0

        base_pad = max(16, self.ch // 48)
        FLOOR = 0.5
        best = None
        scale = 1.0
        while scale >= FLOOR - 1e-9:
            pad = max(6, int(base_pad * scale))
            sublines, fits = [], True
            for path, bsz, text, colour in specs:
                f = self._font_at(path, round(bsz * scale))
                for sub in self._wrap(f, text, max_w):
                    sublines.append((f, sub, colour))
                    if f.size(sub)[0] > max_w:   # a lone word still too wide
                        fits = False
            total_h = (sum(f.get_height() for f, _, _ in sublines)
                       + pad * (len(sublines) - 1))
            if fits and total_h <= max_h:
                best = (sublines, pad)
                break
            scale -= 0.05

        if best is None:
            # Floor scale: ellipsize each wrapped line to max_w, then drop lines
            # that would spill past max_h (ellipsis already on the last kept).
            pad = max(6, int(base_pad * FLOOR))
            flat = []
            for path, bsz, text, colour in specs:
                f = self._font_at(path, round(bsz * FLOOR))
                for sub in self._wrap(f, text, max_w):
                    flat.append((f, self._ellipsize(f, sub, max_w), colour))
            kept, h = [], 0
            for f, sub, colour in flat:
                add = f.get_height() + (pad if kept else 0)
                if kept and h + add > max_h:
                    break
                kept.append((f, sub, colour))
                h += add
            best = (kept, pad)

        sublines, pad = best
        rendered = [(f.render(t, True, c), f.render(t, True, (0, 0, 0)))
                    for f, t, c in sublines]
        return rendered, pad

    def _text_overlay(self, np: NowPlaying):
        """Build (and cache per track) a full-screen SRCALPHA overlay holding
        the scrim + artist/title text, so render() can fade it in/out by
        scaling the overlay's alpha."""
        key = (np.title, np.artist, np.album, self.cfg.show_album)
        if getattr(self, "_ov_key", None) == key:
            return self._ov_surf

        pygame = self.pygame
        pad0 = max(16, self.ch // 48)
        side = max(20, self.cw // 20)                 # keep text off the edges
        max_w = self.cw - 2 * side

        # Vertical budget: in the stacked layout the text must fit the band below
        # the cover (never rise over it); centered layout gets a generous half.
        if self._info_h > 0:
            band_top = getattr(self, "_cur_band_top", self.ch - self._info_h)
            max_h = max(1, (self.ch - band_top) - 2 * pad0)
        else:
            band_top = None
            max_h = int(self.ch * 0.5)

        rendered, pad = self._fit_lines(np, max_w, max_h)
        if not rendered:
            self._ov_key, self._ov_surf = key, None
            return None
        total_h = sum(fg.get_height() for fg, _ in rendered) + pad * (len(rendered) - 1)

        ov = pygame.Surface((self.cw, self.ch), pygame.SRCALPHA)
        if self._info_h > 0:
            # Stacked layout: text centred within the band below the cover. The
            # max() keeps it from ever rising over the cover even if it's tall.
            y = max(band_top, band_top + (self.ch - band_top - total_h) // 2)
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
    cover_cache = {}          # cover_key -> (decoded surface, final)
    # `final` False = the fast 640px variant is cached and a hi-res upgrade is
    # still worthwhile; True = hi-res fetched, or the art has no hi-res form.
    last_key = None
    cover_retry_key = None    # cover_key being re-fetched after a failed attempt
    cover_retry_n = 0         # bounded fast retries for that key (stale-clock etc.)
    prefetch_failed_key = None  # next-cover key that failed; one attempt only
    hires_pending = None      # cover_key awaiting its hi-res upgrade fetch
    art_t0 = None             # art change detected at this time (for the log line)
    art_src = ""              # "prefetched" (cache hit) or "fetched"
    # Radio cover lookup: a found cover lives in radio_surface and out-ranks
    # cover_surface (station art) at render; station-art machinery untouched.
    radio_ident = None        # (artist, albumish) of the armed/shown lookup
    radio_surface = None      # exact-match cover for radio_ident, or None
    radio_stage = None        # None | "search" | ("fetch", big_url, source)
    radio_try_n = 0           # transient-failure retries for this ident
    radio_next_try = 0.0      # earliest monotonic time for the next retry
    radio_backoff_until = 0.0  # global cooldown after ANY transient search
    #                            failure; deliberately NOT reset on ident
    #                            change — a dual-metadata station flipping
    #                            idents every few seconds during a service
    #                            outage must not fire an unpaced search storm
    radio_neg = {}            # ident -> True: search completed, no exact match
    radio_key_idents = {}     # cover_key -> first ident seen with it, or the
    #                           LOGO sentinel once seen with a second ident;
    #                           distinguishes per-song station art (usable as
    #                           a visual-match reference) from station logos
    RADIO_LOGO = object()     # sentinel for "this art key repeats across songs"
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
    COVER_RETRY = 3.0         # re-fetch a failed cover this often (fast)…
    COVER_RETRY_MAX = 20      # …up to this many tries, then rely on the heartbeat
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
                        prefetch_failed_key = None
                        hires_pending = None
                        radio_ident = None
                        radio_surface = None
                        radio_stage = None
                        radio_try_n, radio_next_try = 0, 0.0
                        # radio_neg / radio_key_idents / radio_backoff_until
                        # survive: station/song/service knowledge, not player
                        # state. Backdrop entries age out via the bg FIFO.

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
                            art_t0 = time.monotonic()
                            hires_pending = None       # previous upgrade is moot
                            cached = cover_cache.get(np.cover_key)
                            art_src = "prefetched" if cached is not None else "fetched"
                            if cached is not None:
                                surf, final = cached
                            else:
                                # Fast variant first (~0.5s) so the panel
                                # updates immediately; the hi-res upgrade step
                                # below sharpens it afterwards.
                                surf = _fetch_cover(client, display, np.cover_url)
                                final = not np.cover_hires_url
                                if (surf is None and np.cover_hires_url
                                        and np.cover_key != cover_retry_key):
                                    # First failure for this key only (the
                                    # bounded retry below re-enters here every
                                    # ~3s — one slow fallback, not twenty):
                                    # rare hash with no 640 variant.
                                    surf = _fetch_cover(client, display,
                                                        np.cover_hires_url)
                                    final = True
                                if surf is not None:
                                    cover_cache[np.cover_key] = (surf, final)
                                    _trim_cache(cover_cache)
                            # Adopt the new art; if the fetch failed, drop to
                            # no-cover for this key so we show the status screen
                            # rather than the *previous* track's cover.
                            cover_surface = surf
                            if surf is not None:
                                # Success: stop re-fetching this key.
                                last_key = np.cover_key
                                cover_retry_key = None
                                if not final and np.cover_hires_url:
                                    hires_pending = np.cover_key
                            else:
                                # Fetch failed — e.g. a stale boot clock (no RTC +
                                # ro overlay can't persist fake-hwclock) makes the
                                # cover CDN's TLS cert read "not yet valid" until NTP
                                # syncs. Do NOT advance last_key: keep showing status,
                                # but re-fetch this same key soon instead of caching
                                # the miss until the track changes. Bounded, then we
                                # fall back to the normal heartbeat re-sweep.
                                if np.cover_key != cover_retry_key:
                                    cover_retry_key = np.cover_key
                                    cover_retry_n = 0
                                if cover_retry_n < COVER_RETRY_MAX:
                                    cover_retry_n += 1
                                    heartbeat_at = min(heartbeat_at,
                                                       now + COVER_RETRY)
                                art_t0 = None   # nothing painted; no timing line

                        # --- radio cover lookup: (re)arm on song identity --- #
                        ident = _radio_ident(cfg, np)
                        if ident != radio_ident:
                            radio_ident = ident
                            # No drop_background here: the surface usually
                            # stays in cover_cache (replays re-adopt it), and
                            # dual-metadata stations flip idents A<->B every
                            # few seconds — dropping would recompute a ~1s
                            # backdrop per flip. The bg cache's own FIFO cap
                            # bounds any pinning.
                            radio_surface = None
                            radio_stage = None
                            radio_try_n, radio_next_try = 0, 0.0
                            if ident is not None:
                                cached = cover_cache.get(("radio",) + ident)
                                if cached is not None:
                                    # Replayed song: adopt before this pass's
                                    # render — zero station-logo flash.
                                    radio_surface = cached[0]
                                elif ident not in radio_neg:
                                    radio_stage = "search"
                        if ident is not None and np.cover_key:
                            # Learn whether this station's art is per-song
                            # (usable as a visual-match reference) or a logo
                            # that repeats across songs.
                            seen = radio_key_idents.get(np.cover_key)
                            if seen is None:
                                radio_key_idents[np.cover_key] = ident
                                _trim_cache(radio_key_idents, 64)
                            elif seen is not RADIO_LOGO and seen != ident:
                                # Re-insert (pop first) so an ACTIVE logo's
                                # sentinel refreshes its FIFO position — it
                                # must outlive per-song keys, not age from
                                # the station's first-ever song.
                                radio_key_idents.pop(np.cover_key)
                                radio_key_idents[np.cover_key] = RADIO_LOGO
                        was_playing = True
                    else:
                        was_playing = False

                    state = classify(cfg, np, power,
                                     (radio_surface or cover_surface) is not None)
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
                    # (hires_pending and ALL radio_* state deliberately survive
                    # a poll failure: the heavy-slot `state in _ENGAGED` gates
                    # already block fetches until a sweep succeeds, and keeping
                    # them armed lets the work resume after a one-blip recovery
                    # instead of losing the cover for the rest of the song —
                    # radio re-arms only on ident CHANGE, so a clear here would
                    # be permanent for the current track.)
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
                elif (state in (S_PLAYING, S_PAUSED)
                        and (radio_surface or cover_surface) is not None):
                    alpha = _text_alpha(now, text_until, show_secs, fade)
                    state_id = (state, last_key, alpha, last_track)
                    if state_id != last_state_id or display.blanked:
                        # A radio-song cover out-ranks the station art. Song
                        # changes repaint via last_track; the mid-track swap-in
                        # renders inline from the heavy slot below.
                        display.render(radio_surface or cover_surface, np, alpha)
                        last_state_id = state_id
                        if art_t0 is not None:
                            print(f"art swap painted in "
                                  f"{(time.monotonic() - art_t0) * 1000:.0f}ms "
                                  f"({art_src})", flush=True)
                            art_t0 = None
                elif state == S_UNREACHABLE:
                    # Server/wifi blip: hold whatever is on screen (usually the last
                    # cover) so a brief outage is invisible. Only after the grace —
                    # or if we never had a cover (cold boot) — do we show
                    # "connecting…". Forcing last_state_id=None makes the cover
                    # repaint cleanly once LMS returns.
                    if ((radio_surface or cover_surface) is None
                            or unreachable_since is None
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

            # --- heavy fetches: hi-res upgrade, else radio lookup, else ----- #
            # --- next-track prefetch ---------------------------------------- #
            # At most ONE blocking network op per iteration so the loop never
            # sits out two timeouts back-to-back; events queue in the socket
            # buffer meanwhile and are drained on the next pass. The radio
            # lookup is split into a search pass and a fetch pass for the same
            # reason; the post-slot unpark chains the passes ~0.5s apart.
            heavy_fired = False

            # (a) Upgrade the painted fast cover to its hi-res variant. Skip
            # (pending stays armed) while events are queued — a rapid skip
            # burst must stay responsive, and its later tracks make this
            # upgrade moot anyway. Gate on the freshly classified state, not
            # the sweep-local `engaged` (stale after a poll exception).
            events_queued = False
            if listener.connected:
                try:
                    events_queued = bool(
                        select.select([listener.fileno()], [], [], 0)[0])
                except (OSError, ValueError):
                    pass
            if (hires_pending and hires_pending == np.cover_key == last_key
                    and np.cover_hires_url and state in _ENGAGED
                    and not events_queued):
                # (np.cover_hires_url can be "" for the SAME key when the
                # server re-emits the b273 form — the key is normalized
                # across size classes; keep pending armed for a later 82c1.)
                t0 = time.monotonic()
                surf = _fetch_cover(client, display, np.cover_hires_url)
                heavy_fired = True
                if surf is not None:
                    try:
                        old = cover_surface
                        display.prewarm_background(surf)
                        cover_cache[np.cover_key] = (surf, True)
                        cover_surface = surf
                        if old is not None:
                            display.drop_background(old)
                        # Repaint NOW — nothing else wakes the loop for up to
                        # event_heartbeat (30s) mid-track. Crossfade: same
                        # art, sharper — reads as a focus-pull, not a cut.
                        alpha = _text_alpha(time.monotonic(), text_until,
                                            show_secs, fade)
                        display.crossfade(cover_surface, np, alpha,
                                          cfg.upgrade_fade_seconds)
                        last_state_id = None   # next pass re-derives identity
                        print(f"art hires upgrade painted in "
                              f"{(time.monotonic() - t0) * 1000:.0f}ms",
                              flush=True)
                    except Exception as exc:  # noqa: BLE001
                        print(f"[warn] hires upgrade render failed: {exc}",
                              flush=True)
                        # cover_surface already holds the hi-res frame; force
                        # the render section to repaint it cleanly next pass
                        # (state_id can't tell fast/hires surfaces apart).
                        last_state_id = None
                    hires_pending = None
                else:
                    # Transient failure: keep the fast art, disarm for this
                    # play-through. The cache entry stays (surf, False), so a
                    # later cache hit (album replay) re-arms the upgrade.
                    hires_pending = None

            # (b) Radio cover lookup: search pass, then (next pass) fetch the
            # picked cover through the LMS imageproxy. Guards mirror (a):
            # fresh state, no queued events; plus the retry pacing and a check
            # that the song the stage was armed for is still the one playing.
            elif (radio_stage and state in _ENGAGED and not events_queued
                    and now >= radio_next_try and now >= radio_backoff_until
                    and radio_ident is not None
                    and _radio_ident(cfg, np) == radio_ident):
                heavy_fired = True
                t0 = time.monotonic()
                if radio_stage == "search":
                    res = _radio_cover_search(cfg, np.artist.strip(),
                                              np.album.strip()
                                              or np.title.strip())
                    if res[0] == "ok":
                        radio_stage = ("fetch", res[1])   # candidate list
                        radio_try_n = 0   # fetch gets its own retry budget
                    elif res[0] == "nomatch":
                        # Definitive: the search completed and nothing passed
                        # the exact-match filters. Never re-search this song.
                        radio_neg[radio_ident] = True
                        _trim_cache(radio_neg, 64)
                        radio_stage = None
                        print(f"radio: no exact match for "
                              f"{np.artist} / {np.album or np.title} "
                              f"({(time.monotonic() - t0) * 1000:.0f}ms)",
                              flush=True)
                    else:
                        # Transient (network/TLS/gate hiccup — e.g. stale
                        # pre-NTP boot clock). Bounded, spaced retries; NOT
                        # negative-cached, so a replay hours later starts
                        # fresh even after giving up now. Logged per failure
                        # — an "API gate change?" here must be visible on
                        # its FIRST occurrence, not after the give-up.
                        radio_try_n += 1
                        radio_backoff_until = now + 20.0
                        print(f"radio: search failed "
                              f"(try {radio_try_n}/3): {res[1]}", flush=True)
                        if radio_try_n >= 3:
                            radio_stage = None
                        else:
                            radio_next_try = now + 10.0 * radio_try_n
                            heartbeat_at = min(heartbeat_at,
                                               max(radio_next_try,
                                                   radio_backoff_until))
                else:
                    # Visual verification: when the station provides
                    # per-song art (its key has never been seen with a
                    # different song), the found cover must perceptually
                    # match it. Station logos (key repeats across songs)
                    # and art-less stations skip the gate — and there ONLY
                    # tier-1 (strict text equality) candidates may show:
                    # the looser tiers exist strictly under dHash cover.
                    ref_ok = (cover_surface is not None
                              and np.cover_key not in ("", "current")
                              and radio_key_idents.get(np.cover_key)
                              == radio_ident)
                    cands = radio_stage[1]
                    # Head = first candidate usable RIGHT NOW. The filter is
                    # deliberately non-destructive: ref_ok can be False just
                    # because the station art is mid-retry, and the loose
                    # candidates must still be there when it lands.
                    head = next((i for i, c in enumerate(cands)
                                 if ref_ok or c[2] == 1), None)
                    surf = None
                    if head is not None:
                        big_url, source, tier = cands[head]
                        surf = _fetch_cover(client, display,
                                            _radio_proxy_url(cfg, big_url))
                    if head is None:
                        # Only loose candidates and no visual reference.
                        if (cover_surface is None
                                and np.cover_key not in ("", "current")
                                and radio_key_idents.get(np.cover_key)
                                == radio_ident):
                            # Transient: this song HAS its own art but the
                            # fetch is still retrying — check again shortly.
                            radio_try_n += 1
                            if radio_try_n >= 3:
                                radio_stage = None   # this occurrence only
                            else:
                                radio_next_try = now + 10.0 * radio_try_n
                                heartbeat_at = min(heartbeat_at,
                                                   radio_next_try)
                        else:
                            # Definitive for this song: logo/art-less
                            # station -> loose tiers unusable by design.
                            # Negative-cache like a nomatch, else every
                            # replay (or A<->B metadata flip, which turns
                            # the art key into the LOGO sentinel) would
                            # re-search the aggregator per occurrence.
                            radio_neg[radio_ident] = True
                            _trim_cache(radio_neg, 64)
                            radio_stage = None
                            print("radio: only loose matches and no "
                                  "per-song art to verify against — "
                                  "skipped", flush=True)
                    elif surf is not None:
                        dist = None
                        if ref_ok:
                            dist = _hamming(_dhash(display.pygame, surf),
                                            _dhash(display.pygame,
                                                   cover_surface))
                        if (dist is not None
                                and dist > cfg.radio_cover_match_threshold):
                            # Looks like different artwork than the stream's
                            # own — reject this candidate and move to the
                            # next source's variant (a different edition /
                            # scan may match; one fetch per pass, unpark
                            # chains them). NOT put into radio_neg: on a
                            # logo station's very first song the "reference"
                            # is the not-yet-identified logo itself, and a
                            # permanent reject would poison a correct cover.
                            # By the song's next replay the logo key has been
                            # seen with other idents -> sentinel -> the gate
                            # skips it and the cover goes through. Genuinely
                            # wrong art just gets re-rejected per replay.
                            cands.pop(head)
                            if dist > cfg.radio_cover_match_threshold + 8:
                                # FAR miss: the stores all carry the same
                                # artwork family (observed live: five sources
                                # rejected at an identical distance) — scan
                                # variants differ by a few bits, not ten, so
                                # none of the rest can pass. Fetching them
                                # one by one just grinds for ~2s each.
                                if cands:
                                    print(f"radio: dhash {dist}/64 far off "
                                          f"— skipping {len(cands)} "
                                          f"remaining candidate(s)",
                                          flush=True)
                                cands = []
                            radio_stage = ("fetch", cands) if cands else None
                            radio_try_n = 0   # fresh budget per candidate
                            print(f"radio: cover rejected, dhash {dist}/64 "
                                  f"> {cfg.radio_cover_match_threshold} "
                                  f"({source} tier {tier}"
                                  + (f"; trying {cands[0][1]}" if cands
                                     else "; no more candidates") + ")",
                                  flush=True)
                        else:
                            try:
                                display.prewarm_background(surf)
                                cover_cache[("radio",) + radio_ident] = (surf,
                                                                         True)
                                _trim_cache(cover_cache)
                                radio_surface = surf
                                alpha = _text_alpha(time.monotonic(),
                                                    text_until, show_secs,
                                                    fade)
                                display.crossfade(radio_surface, np, alpha,
                                                  cfg.upgrade_fade_seconds)
                                last_state_id = None
                                print(f"radio cover painted in "
                                      f"{(time.monotonic() - t0) * 1000:.0f}"
                                      f"ms ({source} tier {tier}"
                                      + (f", dhash {dist}/64" if dist
                                         is not None else "") + ")",
                                      flush=True)
                            except Exception as exc:  # noqa: BLE001
                                print(f"[warn] radio cover render failed: "
                                      f"{exc}", flush=True)
                                last_state_id = None
                            radio_stage = None
                    else:
                        # Imageproxy/decode failure: keep the search result,
                        # retry just the fetch on the same pacing; after the
                        # budget, fall through to the next candidate (a
                        # different CDN may serve fine).
                        radio_try_n += 1
                        radio_backoff_until = now + 20.0
                        if radio_try_n >= 3:
                            cands.pop(head)
                            radio_stage = ("fetch", cands) if cands else None
                            radio_try_n = 0
                            print("radio: cover fetch gave up on "
                                  f"{source}"
                                  + (f"; trying {cands[0][1]}" if cands
                                     else "") , flush=True)
                        else:
                            radio_next_try = now + 10.0 * radio_try_n
                            heartbeat_at = min(heartbeat_at,
                                               max(radio_next_try,
                                                   radio_backoff_until))

            # (c) Prefetch the upcoming track's cover + backdrop. Hi-res
            # directly — its cost is invisible here — falling back to the
            # fast variant so a hi-res hiccup still yields instant swaps.
            elif (playing and np.next_cover_key
                    and np.next_cover_key != np.cover_key
                    and np.next_cover_key not in cover_cache
                    and np.next_cover_key != prefetch_failed_key):
                surf = None
                final = True
                if np.next_cover_hires_url:
                    surf = _fetch_cover(client, display,
                                        np.next_cover_hires_url)
                if surf is None:
                    surf = _fetch_cover(client, display, np.next_cover_url)
                    final = not np.next_cover_hires_url
                if surf is not None:
                    cover_cache[np.next_cover_key] = (surf, final)
                    _trim_cache(cover_cache)
                    try:
                        display.prewarm_background(surf)
                    except Exception as exc:  # noqa: BLE001
                        print(f"[warn] backdrop prewarm failed: {exc}",
                              flush=True)
                else:
                    # One attempt per key: the normal change-path (with its
                    # bounded retry) covers it if this track actually plays.
                    prefetch_failed_key = np.next_cover_key

            if heavy_fired:
                # This pass's fetch budget is spent; wake shortly so the next
                # queued heavy step (radio search→fetch chain, or a prefetch
                # parked behind an upgrade) isn't stuck until the heartbeat.
                heartbeat_at = min(heartbeat_at, time.monotonic() + 0.5)

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


def _trim_cache(cache: dict, limit: int = 5):
    # 5 × ~5.5MB surfaces (everything is 1200px-class now that hi-res upgrades
    # replace the 640s) keeps steady-state RSS comfortably under MemoryMax.
    # FIFO by INSERTION: re-storing an existing key (the hi-res upgrade does
    # this) keeps its original position, so upgraded entries age from their
    # first insert — intentional; do not assume re-store refreshes recency.
    while len(cache) > limit:
        cache.pop(next(iter(cache)))


# --------------------------------------------------------------------------- #
# Radio cover search (covers.musichoarders.xyz)
# --------------------------------------------------------------------------- #
# The site has no public API; these headers mimic its own web app (it rejects
# plainly-identified clients). Volume here is tiny — at most one ~1KB search
# per radio song, negative-cached — but the endpoint may still change or block
# us at any time, so every failure path below degrades to "keep station art".
_RADIO_SEARCH_URL = "https://covers.musichoarders.xyz/api/search"
_RADIO_SESSION = os.urandom(16).hex()   # one browser-like session per process
_RADIO_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:151.0) "
                   "Gecko/20100101 Firefox/151.0"),
    "Accept": "*/*",
    "Accept-Language": "fi-FI,fi;q=0.9,en-US;q=0.8,en;q=0.7",
    "Content-Type": "application/json",
    "Referer": "https://covers.musichoarders.xyz/",
    "Origin": "https://covers.musichoarders.xyz",
    "x-session": _RADIO_SESSION,
    # Both x-page-* headers are part of the bot gate: without them the server
    # answers "Please do not use the internal API directly."
    "x-page-referrer": "https://www.google.com/",
    "x-page-query": "",
}
_RADIO_VIDEO_EXT = (".mp4", ".m4v", ".mov", ".webm")
_RADIO_BYTE_CAP = 256 * 1024   # sanity cap on the JSONL stream


def _radio_ident(cfg: Config, np: "NowPlaying"):
    """Search identity (artist, albumish) for the current radio track, or
    None when the track is not radio-cover eligible. Spotify-via-Spotty is
    also remote:1 but is excluded — its art is handled by the hi-res upgrade
    path (cover_hires_url non-empty for both scdn emission directions), with
    a belt check on the raw key for unrecognized scdn size classes."""
    if not (cfg.radio_cover_search and np.remote and np.artist.strip()):
        return None
    if np.cover_hires_url or "i.scdn.co" in np.cover_key:
        return None
    albumish = np.album.strip()
    if not albumish and cfg.radio_cover_title_fallback:
        albumish = np.title.strip()
    if not albumish:
        return None
    return (np.artist.strip().casefold(), albumish.casefold())


def _radio_norm(s: str) -> str:
    """Punctuation-insensitive form for tier-2/3 title comparison: casefold,
    drop apostrophes/quotes and sentence punctuation, collapse whitespace.
    Hyphens/parens/brackets are KEPT (they delimit tier-3 decorations), and
    so are diacritics (ä is not a in Finnish)."""
    s = s.casefold()
    for ch in "'\"’‘“”!?.,":
        s = s.replace(ch, "")
    return " ".join(s.split())


_TIER3_DELIMS = ("(", "[", "-", "–", ":")
# Multi-artist credit separators ("Alok, Zeeba & Portugal. The Man"). " and "
# is deliberately absent — it is part of too many band names.
_CREDIT_SEPS = (",", ";", "&", " feat.", " feat ", " featuring ", " ft.",
                " ft ", " with ", " w/ ", " x ", " × ")


def _credit_segments(artist_raw: str):
    """Normalized set of individual artists in a (possibly multi-artist)
    credit string: "Alok, Zeeba & Portugal. The Man" ->
    {"alok", "zeeba", "portugal the man"}. Split BEFORE normalizing —
    _radio_norm strips the commas the split needs."""
    s = artist_raw.casefold()
    for sep in _CREDIT_SEPS:
        s = s.replace(sep, "|")
    return {seg for seg in (_radio_norm(p) for p in s.split("|")) if seg}


def _radio_tier(artist_q: str, album_q: str, artist_r: str, title_r: str):
    """Match tier of one search result against the query: 1 = exact
    (casefold equality, the original strict rule), 2 = punctuation-
    insensitive equality, 3 = decorated variant of the same release
    ("Album (Deluxe)", "Album - Single", store artist "Artist(아티스트)",
    or the reverse — stream album decorated, store title plain). 0 = no
    match. Tiers 2/3 are only ever shown after the dHash visual gate."""
    a_q, t_q = artist_q.strip().casefold(), album_q.strip().casefold()
    a_r, t_r = (artist_r or "").strip().casefold(), (title_r or "").strip().casefold()
    if not a_r or not t_r:
        return 0
    if a_q == a_r and t_q == t_r:
        return 1
    na_q, nt_q = _radio_norm(artist_q), _radio_norm(album_q)
    na_r, nt_r = _radio_norm(artist_r), _radio_norm(title_r)
    if not (na_q and nt_q and na_r and nt_r):
        # Punctuation-only names ("?", "!!!") normalize to "" and would make
        # every startswith below vacuously true. Their legitimate matches are
        # literal and already returned tier 1 above.
        return 0
    artist_ok = (na_q == na_r
                 or (na_r.startswith(na_q)
                     and na_r[len(na_q):].lstrip().startswith("(")))
    # Multi-artist credits: radio tags the primary artist while stores credit
    # everyone ("Portugal. The Man" vs "Alok, Zeeba & Portugal. The Man") —
    # match when either side is a complete segment of the other's credit
    # list. Loose by nature -> tier-3 class (only shown after dHash).
    artist_credit = (not artist_ok
                     and (na_q in _credit_segments(artist_r)
                          or na_r in _credit_segments(artist_q)))
    if not artist_ok and not artist_credit:
        return 0
    title_eq = nt_q == nt_r
    title_dec = False
    if not title_eq:
        for longer, shorter in ((nt_r, nt_q), (nt_q, nt_r)):
            if longer.startswith(shorter):
                rest = longer[len(shorter):].lstrip()
                if rest and rest.startswith(_TIER3_DELIMS):
                    title_dec = True
                    break
    if not title_eq and not title_dec:
        return 0
    if artist_credit:
        return 3
    return 2 if title_eq else 3


def _radio_cover_search(cfg: Config, artist: str, album: str):
    """Query the cover aggregator for artist+album. Returns
    ("ok", [(big_url, source, tier), ...])  matches, best first, max 5,
    ("nomatch",)   when the search completed without one (definitive),
    ("error", reason)  on any transport/gate problem (transient).

    Sources return relevance-ranked result lists that include entirely
    unrelated releases, so every result is tiered client-side against the
    query (see _radio_tier) — the API's count "accuracy" field describes
    the RESULT-COUNT precision, not match quality, and is ignored. Order:
    tier, then config source preference, then the source's own ranking.
    Video "covers" (Apple motion art, .mp4) are skipped."""
    body = json.dumps({"artist": artist, "album": album,
                       "country": cfg.radio_cover_country,
                       "sources": cfg.radio_cover_sources}).encode()
    req = urllib.request.Request(_RADIO_SEARCH_URL, data=body,
                                 headers=_RADIO_HEADERS)
    deadline = time.monotonic() + cfg.radio_cover_timeout
    results = {}      # source -> [(url, tier), ...] in the source's own order
    done = set()
    saw_events = False
    complete = False  # stream ended on the server's terms (EOF / all done)
    try:
        with urllib.request.urlopen(req, timeout=cfg.radio_cover_timeout) as r:
            seen = 0
            while True:
                # Bounded read: a plain `for raw in r` blocks in an UNLIMITED
                # readline — a slow-drip response without newlines would hang
                # the whole loop past any deadline. Capping the read length
                # and re-checking the wall deadline after every read keeps
                # the worst stall ~one socket timeout. An over-long line
                # arrives fragmented, fails json.loads and is skipped.
                raw = r.readline(8192)
                if not raw:
                    # Natural EOF: the server said all it will. Sources can
                    # miss their done event (server-side outage) — still
                    # definitive PROVIDED the top-preference source concluded
                    # (it decides the pick anyway); a zero-done stream is the
                    # bot-gate refusal text (non-JSONL) or a gutted response,
                    # which must stay transient and get flagged.
                    complete = cfg.radio_cover_sources[0] in done
                    break
                seen += len(raw)
                if seen > _RADIO_BYTE_CAP or time.monotonic() > deadline:
                    break
                try:
                    ev = json.loads(raw)
                except ValueError:
                    continue
                src = ev.get("source", "")
                typ = ev.get("type")
                if typ in ("count", "done", "cover", "source"):
                    saw_events = True
                if typ == "done":
                    done.add(src)
                elif typ == "cover" and len(results.get(src, ())) < 10:
                    info = ev.get("releaseInfo") or {}
                    url = ev.get("bigCoverUrl") or ""
                    path = url.split("?", 1)[0].lower()
                    if (url.startswith("http")
                            and not path.endswith(_RADIO_VIDEO_EXT)
                            and "mvod.itunes.apple.com" not in url):
                        tier = _radio_tier(artist, album,
                                           info.get("artist") or "",
                                           info.get("title") or "")
                        if tier and (tier == 1
                                     or cfg.radio_cover_loose_match):
                            results.setdefault(src, []).append((url, tier))
                if all(s in done for s in cfg.radio_cover_sources):
                    complete = True
                    break
    except Exception as exc:  # noqa: BLE001  (URLError, SSL, timeout, HTTP…)
        return ("error", str(exc))
    # Best first: tier, then config source preference, then the source's own
    # relevance order; dedup by URL (stores repeat editions).
    cands, seen_urls = [], set()
    for tier in (1, 2, 3):
        for src in cfg.radio_cover_sources:
            for url, t in results.get(src, ()):
                if t == tier and url not in seen_urls:
                    seen_urls.add(url)
                    cands.append((url, src, tier))
    cands = cands[:5]
    if cands:
        return ("ok", cands)
    if complete:
        return ("nomatch",)
    if not saw_events:
        # Loudly distinguishable: header mimicry may have stopped working.
        return ("error", "no sources answered (API gate change?)")
    return ("error", "search cut short (deadline/cap)")


def _radio_proxy_url(cfg: Config, big_url: str) -> str:
    # Fetch the picked cover through the LMS imageproxy: the Pi only does
    # plain LAN HTTP (clock-independent, see _art_url) and LMS resizes the
    # often-huge original server-side.
    px = cfg.cover_px
    return (f"{cfg.base_url}/imageproxy/"
            f"{quote(big_url, safe='')}/image_{px}x{px}_o.png")


def _dhash(pygame, surf) -> int:
    """64-bit perceptual difference hash: center-crop to square, shrink to
    9×8 grayscale, hash the horizontal brightness gradient. Same artwork at
    different sizes/compressions lands within a few bits; unrelated images
    differ by ~25+ of 64."""
    w, h = surf.get_size()
    side = min(w, h)
    if side <= 0:
        return 0
    sq = surf.subsurface(((w - side) // 2, (h - side) // 2, side, side))
    tiny = pygame.transform.smoothscale(sq, (9, 8))
    bits = 0
    for y in range(8):
        prev = None
        for x in range(9):
            r, g, b = tiny.get_at((x, y))[:3]
            lum = r + g + b
            if prev is not None:
                bits = (bits << 1) | (1 if lum > prev else 0)
            prev = lum
    return bits


def _hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


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
