"""Restream any radio source (Icecast, PLS/M3U, HLS, TuneIn) as one steady HTTP stream.

Players get what a plain Icecast server would give them: an HTTP/1.0 response
with a raw, never-ending body (no length, no chunking), delivered at real-time
speed after a short start-up burst. However the station publishes, even HLS
that arrives in large segments, the player sees one steady radio stream.

Upstream trouble (expired signed URLs, dropped connections, ffmpeg exits) is
handled here by re-resolving the source and restarting ffmpeg inside the same
response, so the player never sees it.

Endpoints:
  GET /s/<name>                      station from stations.yaml
  GET /play?src=<url|tunein>&fmt=..  ad-hoc source (same options as a station)
  GET /status                        who is listening and how it is going (JSON)
  GET /health                        liveness
"""

import collections
import contextlib
import fcntl
import json
import logging
import os
import re
import signal
import socket
import struct
import subprocess
import sys
import termios
import threading
import time
from dataclasses import dataclass, field, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

import requests
import yaml

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s [restreamer] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("restreamer")


def _load_stations() -> dict:
    """Load station definitions without crashing the app on missing/malformed files."""
    path = os.environ.get("STATIONS_FILE", "/data/stations.yaml")
    try:
        with open(path, "r") as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError:
        # Empty set keeps the app running (returns 404 for any station).
        log.warning("stations file not found at %s; no stations loaded", path)
        return {}
    except Exception as e:
        log.warning("failed to parse stations file %s: %s", path, e)
        return {}

    if not isinstance(data, dict):
        log.warning("stations file %s must contain a mapping; got %s", path, type(data))
        return {}

    return data


# Load station definitions
STATIONS = _load_stations()

# Copy formats pass the source's audio through untouched (AAC in, AAC out).
COPY_FORMATS = {"adts", "mpegts", "mp4"}
# Encoded formats decode the source and re-encode it, so every station comes
# out identical regardless of what it publishes.
ENCODED_FORMATS = {"flac", "wav", "mp3"}

# MIME types for output formats
CTYPES = {
    "mp4": "audio/mp4",
    "mpegts": "video/MP2T",
    "adts": "audio/aac",
    "wav": "audio/wav",
    "flac": "audio/flac",
    "mp3": "audio/mpeg",
}

DEFAULT_FMT = os.environ.get("DEFAULT_FMT", "adts").lower()
# Seconds of audio sent straight away on connect; after that, real time. Like
# Icecast's burst-on-connect: enough for the player to start promptly without
# handing it a large backlog.
BURST_SECONDS = float(os.environ.get("BURST_SECONDS", "4"))
UA = os.environ.get("UA", "VLC/3.0")
HTTP_TIMEOUT = (3, 10)  # (connect, read) seconds
HLS_RE = re.compile(r"\.m3u8($|\?)", re.I)
HDNEA_RE = re.compile(r"(?i)\bhdnea=[^;]+")

# Restart policy: after a healthy run, restart at once (the player only holds
# a few seconds of audio); back off only across repeated failures, and give
# up after repeated attempts that never produced audio.
RESTART_BACKOFF_S = (0, 1, 2, 4, 8, 10)
MAX_FAILED_STARTS = 5
HEALTHY_AFTER_S = 30  # a run this long resets the failure count

# ---------- TuneIn ----------
TUNEIN_URL = (
    "https://opml.radiotime.com/Tune.ashx"
    "?id={station_id}&partnerId=RadioTime&version=5.38&listenId=1"
    "&formats=mp3,aac,ogg,hls&type=station&render=json"
)
# TuneIn's placeholder for "your client cannot play this station".
TUNEIN_PLACEHOLDER = "notcompatible"
TUNEIN_ID_RE = re.compile(r"^(?:tunein:)?([sgpt]\d+)$", re.I)
TUNEIN_URL_RE = re.compile(r"tunein\.com/.*?\b([sgpt]\d+)\b", re.I)


def tunein_station_id(src: str) -> str | None:
    """Return a TuneIn station ID if src is 'tunein:s123', 's123' or a tunein.com URL."""
    s = src.strip()
    m = TUNEIN_ID_RE.match(s)
    if m:
        return m.group(1).lower()
    m = TUNEIN_URL_RE.search(s)
    if m:
        return m.group(1).lower()
    return None


def resolve_tunein(station_id: str) -> str:
    """Ask TuneIn for a playable stream URL. Each call returns a freshly signed URL."""
    r = requests.get(
        TUNEIN_URL.format(station_id=station_id),
        headers={"User-Agent": UA},
        timeout=HTTP_TIMEOUT,
    )
    r.raise_for_status()
    body = (r.json() or {}).get("body") or []

    entries = [
        e for e in body
        if isinstance(e, dict)
        and (e.get("url") or "").strip()
        and TUNEIN_PLACEHOLDER not in e["url"].lower()
    ]
    if not entries:
        raise ValueError(f"TuneIn has no playable stream for {station_id}")

    # Highest bitrate wins; TuneIn otherwise orders by the formats we asked for.
    def bitrate(e: dict) -> int:
        try:
            return int(e.get("bitrate") or 0)
        except (TypeError, ValueError):
            return 0

    return max(entries, key=bitrate)["url"].strip()


# ---------- helpers: playlist/redirect resolver (fresh session URL per request) ----------
def _first_url_from_pls(text: str) -> str | None:
    for line in text.splitlines():
        s = line.strip()
        if s.lower().startswith("file") and "=" in s:
            u = s.split("=", 1)[1].strip()
            if u.lower().startswith("http"):
                return u
    return None


def _first_url_from_m3u(text: str) -> str | None:
    for line in text.splitlines():
        s = line.strip()
        if s and not s.startswith("#") and s.lower().startswith("http"):
            return s
    return None


def _resolve_url(base: str, rel: str) -> str:
    if re.match(r"^https?://", rel):
        return rel
    return base.rsplit("/", 1)[0] + "/" + rel.lstrip("/")


def _prime_cookie(session: requests.Session, url: str) -> str | None:
    """Hit the URL to capture any Set-Cookie (e.g., hdnea)."""
    try:
        r = session.head(url, allow_redirects=True, timeout=HTTP_TIMEOUT)
    except Exception:
        r = None
    if not r or r.status_code >= 400:
        r = session.get(url, allow_redirects=True, timeout=HTTP_TIMEOUT)

    # Prefer cookies from the jar (most reliable)
    for c in session.cookies:
        if c.name.lower() == "hdnea":
            return f"{c.name}={c.value}"

    # Fallback: parse literal Set-Cookie (if server didn't put it in the jar)
    sc = (r.headers.get("Set-Cookie") if r else None) or ""
    m = HDNEA_RE.search(sc)
    return m.group(0) if m else None


def _pick_best_child_from_master(master_text: str, master_url: str) -> tuple[str, int]:
    """
    Parse #EXT-X-STREAM-INF; choose the child with highest BANDWIDTH.
    Ties go to the first listed, which is the provider's own preference.
    Returns (child_url, bandwidth). Raises on failure.
    """
    lines = [ln.strip() for ln in master_text.splitlines() if ln.strip()]
    best_bw = -1
    best_child = None
    for i, ln in enumerate(lines):
        if ln.upper().startswith("#EXT-X-STREAM-INF"):
            m = re.search(r"BANDWIDTH=(\d+)", ln, re.I)
            bw = int(m.group(1)) if m else -1
            # next non-comment line is the child playlist URL
            j = i + 1
            while j < len(lines) and lines[j].startswith("#"):
                j += 1
            if j < len(lines):
                child = _resolve_url(master_url, lines[j])
                if bw > best_bw:
                    best_bw, best_child = bw, child
    if not best_child:
        raise ValueError("No child playlists found in master")
    return best_child, best_bw


def resolve_once(url: str, timeout=12) -> str:
    """Follow redirects; for playlists fetch body, for live streams avoid downloading the body."""
    headers = {"User-Agent": UA}
    path = urlparse(url).path.lower()

    # PLS → extract first URL
    if path.endswith(".pls") or "format=pls" in url.lower():
        r = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
        r.raise_for_status()
        u = _first_url_from_pls(r.text)
        if not u:
            raise ValueError("No FileN= URL in PLS")
        return u

    # M3U (not M3U8) → extract first URL
    if path.endswith(".m3u") and not path.endswith(".m3u8"):
        r = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
        r.raise_for_status()
        u = _first_url_from_m3u(r.text)
        if not u:
            raise ValueError("No URL in M3U")
        return u

    # M3U8 (master/media) → handled in choose_hls_best, but resolving is safe
    if path.endswith(".m3u8"):
        r = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
        r.raise_for_status()
        return r.url

    # Everything else (e.g., Icecast AAC/MP3): DO NOT read the body.
    # Try HEAD first (fast), fall back to GET(stream=True) and close.
    try:
        rh = requests.head(url, headers=headers, timeout=timeout, allow_redirects=True)
        rh.raise_for_status()
        return rh.url
    except Exception:
        rg = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True, stream=True)
        try:
            rg.raise_for_status()
            return rg.url
        finally:
            rg.close()


def choose_hls_best(url: str) -> tuple[str, list[str]]:
    """
    Given a (possibly master) HLS URL:
      - prime cookies,
      - if it's a master, select highest-bandwidth child,
      - return (input_url_for_ffmpeg, extra_ffmpeg_header_args)
    """
    s = requests.Session()
    s.headers["User-Agent"] = UA

    # 1) prime cookie if any (never fatal)
    cookie = None
    try:
        cookie = _prime_cookie(s, url)
    except Exception:
        cookie = None

    # 2) fetch the (master) playlist
    r = s.get(url, allow_redirects=True, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    final_url = r.url
    text = r.text

    # If it looks like a master (has EXT-X-STREAM-INF), choose the best child; otherwise just use this URL
    if "#EXT-X-STREAM-INF" in text:
        child, bw = _pick_best_child_from_master(text, final_url)
        # quick preflight on child, with cookie if we have one
        hdrs = {"User-Agent": UA}
        if cookie:
            hdrs["Cookie"] = cookie
        rc = s.get(child, headers=hdrs, timeout=HTTP_TIMEOUT)
        rc.raise_for_status()
        in_url = child
    else:
        in_url = final_url  # already a media playlist

    extra = []
    if cookie:
        extra += ["-headers", f"Cookie: {cookie}"]
    return in_url, extra


def resolve_source(src: str) -> tuple[str, list[str]]:
    """Turn a station source into (ffmpeg input URL, extra ffmpeg args).

    Called again on every restart, so signed or session URLs are always fresh.
    """
    station_id = tunein_station_id(src)
    url = resolve_tunein(station_id) if station_id else src

    resolved = resolve_once(url)
    if HLS_RE.search(resolved):
        try:
            return choose_hls_best(resolved)
        except Exception as e:
            # Fall back to the resolved URL; ffmpeg can often still play it.
            log.warning("HLS variant selection failed, using playlist as-is: %s", e)
    return resolved, []


def _redact(url: str) -> str:
    """Drop the query string, which often carries signatures, from logged URLs."""
    return url.split("?", 1)[0]


# ---------- ffmpeg command builders ----------
def _input_args(url: str, extra: list[str], *, probe_fast: bool) -> list[str]:
    # Copying needs no decoding, so it can start on the first packet. Decoding
    # probes a little longer so HE-AAC streams report their real sample rate
    # rather than the core rate.
    probe = (
        ["-analyzeduration", "0", "-probesize", "32k"]
        if probe_fast
        else ["-analyzeduration", "2000000", "-probesize", "512k"]
    )
    return [
        "-user_agent", UA,
        "-headers", "Icy-MetaData: 1",
        *extra,
        *probe,
        # Deliver like a radio server: a short burst, then exactly real time,
        # rather than passing on whatever arrives (HLS comes a whole segment
        # at once).
        "-readrate", "1",
        "-readrate_initial_burst", f"{BURST_SECONDS:g}",
        "-fflags", "+nobuffer",
        "-rw_timeout", "15000000",
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_on_network_error", "1",
        "-reconnect_delay_max", "5",
        "-i", url,
    ]


def copy_cmd(url: str, extra: list[str], fmt: str) -> list[str]:
    """Single ffmpeg that passes the source audio straight through."""
    base = [
        "ffmpeg", "-hide_banner", "-nostdin", "-nostats", "-loglevel", "level+info",
        *_input_args(url, extra, probe_fast=True),
        "-map", "0:a:0", "-vn", "-sn", "-dn",
    ]

    if fmt == "mp4":
        return base + [
            "-c:a", "copy", "-bsf:a", "aac_adtstoasc",
            "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
            "-muxdelay", "0", "-muxpreload", "0",
            "-f", "mp4", "-",
        ]

    if fmt == "mpegts":
        return base + [
            "-c:a", "copy",
            "-muxdelay", "0", "-muxpreload", "0",
            "-f", "mpegts", "-",
        ]

    # adts
    return base + [
        "-c:a", "copy",
        "-flush_packets", "1",
        "-muxdelay", "0", "-muxpreload", "0",
        "-f", "adts", "-",
    ]


@dataclass
class PcmFormat:
    """Raw PCM handed from the decoder to the encoder."""
    rate: int
    channels: int
    bits: int  # 16 or 24 (24-bit travels as s32)

    @property
    def ffmpeg_fmt(self) -> str:
        return "s16le" if self.bits == 16 else "s32le"


def decode_cmd(url: str, extra: list[str], *, bits: int, channels: int, rate: int | None) -> list[str]:
    """Decoder: source → raw PCM on stdout. Restartable without the player noticing."""
    args = [
        "ffmpeg", "-hide_banner", "-nostdin", "-nostats", "-loglevel", "level+info",
        *_input_args(url, extra, probe_fast=False),
        "-map", "0:a:0", "-vn", "-sn", "-dn",
        "-ac", str(channels),
    ]
    if rate:
        args += ["-ar", str(rate)]
    fmt = "s16le" if bits == 16 else "s32le"
    return args + ["-c:a", f"pcm_{fmt}", "-f", fmt, "-"]


# MP3 at 320 kbps exists only at these sample rates (MPEG-1 Layer III).
MP3_RATES = {32000, 44100, 48000}
MP3_BITRATE = "320k"


def encode_cmd(pcm: PcmFormat, fmt: str) -> list[str]:
    """Encoder: raw PCM on stdin → one continuous output stream. Lives for the whole session."""
    base = [
        "ffmpeg", "-hide_banner", "-nostats", "-loglevel", "level+warning",
        "-f", pcm.ffmpeg_fmt, "-ar", str(pcm.rate), "-ac", str(pcm.channels), "-i", "pipe:0",
    ]

    if fmt == "wav":
        codec = "pcm_s16le" if pcm.bits == 16 else "pcm_s24le"
        return base + ["-c:a", codec, "-flush_packets", "1", "-f", "wav", "-"]

    if fmt == "mp3":
        # CBR, and no Xing/Info or ID3 header: in a live stream those describe
        # a "file" of zero length, which some players take literally.
        args = [
            "-c:a", "libmp3lame", "-b:a", MP3_BITRATE,
            "-write_xing", "0", "-id3v2_version", "0",
            "-flush_packets", "1",
        ]
        if pcm.rate not in MP3_RATES:
            args += ["-ar", "48000"]
        return base + args + ["-f", "mp3", "-"]

    # flac
    args = ["-c:a", "flac", "-compression_level", "5", "-flush_packets", "1"]
    if pcm.bits == 24:
        # ffmpeg has no s24 sample format; FLAC 24-bit is s32 with 24 significant bits.
        args += ["-sample_fmt", "s32", "-bits_per_raw_sample", "24"]
    else:
        args += ["-sample_fmt", "s16"]
    return base + args + ["-f", "flac", "-"]


# ---------- process plumbing ----------
_OUTPUT_AUDIO_RE = re.compile(r"Audio: pcm_\w+, (\d+) Hz")
_INPUT_AUDIO_RE = re.compile(r"Stream #\d+:\d+.*?: Audio: (\w+)")


class FFmpegProc:
    """An ffmpeg subprocess whose stderr is logged (warnings and errors) and kept for diagnosis."""

    def __init__(self, cmd: list[str], label: str, *, stdin=None, watch_output_rate: bool = False):
        self.label = label
        self.started = time.time()
        self.tail: list[str] = []
        self.output_rate: int | None = None
        self.input_codec: str | None = None
        self.rate_known = threading.Event()
        self._watch_rate = watch_output_rate
        self._in_output_section = False
        self.proc = subprocess.Popen(
            cmd,
            stdin=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        threading.Thread(target=self._drain_stderr, daemon=True).start()

    def _drain_stderr(self) -> None:
        # Keep ffmpeg's stderr pipe empty so it can't block under error spam.
        try:
            for raw in iter(self.proc.stderr.readline, b""):
                line = raw.decode(errors="replace").rstrip()
                if not line:
                    continue
                self.tail = (self.tail + [line])[-10:]

                if self.input_codec is None and not self._in_output_section and "Output #0" not in line:
                    m = _INPUT_AUDIO_RE.search(line)
                    if m:
                        self.input_codec = m.group(1)
                if "Output #0" in line:
                    self._in_output_section = True

                if self._watch_rate and not self.rate_known.is_set():
                    if self._in_output_section:
                        m = _OUTPUT_AUDIO_RE.search(line)
                        if m:
                            self.output_rate = int(m.group(1))
                            self.rate_known.set()

                if line.startswith(("[error]", "[fatal]", "[panic]")):
                    log.error("%s ffmpeg: %s", self.label, line)
                elif line.startswith("[warning]"):
                    log.warning("%s ffmpeg: %s", self.label, line)
        except Exception:
            pass
        finally:
            self.rate_known.set()  # unblock anyone waiting, even if we never saw it

    @property
    def stdout(self):
        return self.proc.stdout

    def alive(self) -> bool:
        return self.proc.poll() is None

    def stop(self) -> None:
        with contextlib.suppress(Exception):
            self.proc.terminate()
        try:
            self.proc.wait(timeout=2)
        except Exception:
            with contextlib.suppress(Exception):
                os.kill(self.proc.pid, signal.SIGKILL)


@dataclass
class StreamSpec:
    name: str
    src: str
    fmt: str
    bits: int = 16
    channels: int = 2
    rate: int | None = None  # None = pass the source rate through
    extra: dict = field(default_factory=dict)


class Restarter:
    """Tracks restart attempts: backs off, and gives up only on repeated dead starts."""

    def __init__(self, label: str):
        self.label = label
        self.failed_starts = 0
        self.restarts = 0

    def run_ended(self, ran_for: float, produced: bool) -> bool:
        """Record a finished run. Returns False if we should give up."""
        if produced and ran_for >= HEALTHY_AFTER_S:
            self.failed_starts = 0
        elif not produced:
            self.failed_starts += 1
        if self.failed_starts >= MAX_FAILED_STARTS:
            log.error("%s giving up after %d failed starts", self.label, self.failed_starts)
            return False
        return True

    def wait_before_restart(self, stop: threading.Event) -> None:
        delay = RESTART_BACKOFF_S[min(self.failed_starts, len(RESTART_BACKOFF_S) - 1)]
        self.restarts += 1
        log.info("%s restarting source in %ss (restart #%d)", self.label, delay, self.restarts)
        if delay:
            stop.wait(delay)


CHUNK = 64 * 1024


class AdtsFramer:
    """Pass on whole ADTS (AAC) frames only.

    ffmpeg writes whole frames while it runs, but a source restart can leave a
    cut frame at the seam. A player's decoder can choke on that, so a frame is
    only sent once all of it has arrived, and a cut one is dropped. Anything
    that isn't a frame is skipped up to the next valid header.
    """

    HEADER = 7

    def __init__(self) -> None:
        self._buf = bytearray()
        self.skipped = 0

    @staticmethod
    def _frame_len(b: bytearray, i: int) -> int:
        """Frame length if a valid ADTS header starts at i, else 0."""
        if b[i] != 0xFF or (b[i + 1] & 0xF6) != 0xF0:  # 12-bit sync, layer 00
            return 0
        if (b[i + 2] >> 2) & 0x0F > 12:  # sampling-frequency index out of range
            return 0
        length = ((b[i + 3] & 0x03) << 11) | (b[i + 4] << 3) | (b[i + 5] >> 5)
        return length if length >= AdtsFramer.HEADER else 0

    def feed(self, data: bytes) -> bytes:
        buf = self._buf
        buf += data
        out = bytearray()
        i, n = 0, len(buf)
        while n - i >= self.HEADER:
            length = self._frame_len(buf, i)
            if not length:
                i += 1
                self.skipped += 1
                continue
            if n - i < length:
                break  # rest of this frame hasn't arrived yet
            out += buf[i:i + length]
            i += length
        del buf[:i]
        return bytes(out)

    def drop_partial(self) -> int:
        """Discard an incomplete trailing frame (at a source restart). Returns bytes dropped."""
        dropped = len(self._buf)
        self.skipped += dropped
        self._buf.clear()
        return dropped


def stream_copy(spec: StreamSpec, label: str):
    """Yield the source's own audio, restarting ffmpeg in place when it exits."""
    stop = threading.Event()
    restarter = Restarter(label)
    proc: FFmpegProc | None = None
    framer = AdtsFramer() if spec.fmt == "adts" else None
    try:
        while not stop.is_set():
            try:
                url, extra = resolve_source(spec.src)
            except Exception as e:
                log.warning("%s resolve failed: %s", label, e)
                if not restarter.run_ended(0, produced=False):
                    return
                restarter.wait_before_restart(stop)
                continue

            log.info("%s source %s (copy → %s)", label, _redact(url), spec.fmt)
            proc = FFmpegProc(copy_cmd(url, extra, spec.fmt), label)
            produced = False
            while True:
                chunk = proc.stdout.read(CHUNK)
                if not chunk:
                    break
                produced = True
                if framer is not None:
                    chunk = framer.feed(chunk)
                    if not chunk:
                        continue
                yield chunk

            proc.stop()
            if framer is not None:
                dropped = framer.drop_partial()
                if dropped:
                    log.info("%s dropped a cut frame (%d bytes) at the source seam", label, dropped)
            ran_for = time.time() - proc.started
            if not produced and spec.fmt in ("adts", "mp4") and proc.input_codec not in (None, "aac"):
                log.error(
                    "%s source audio is %s; fmt %s only passes AAC through. Use fmt: flac for this station.",
                    label, proc.input_codec, spec.fmt,
                )
                return
            log.warning(
                "%s ffmpeg ended after %.0fs (exit=%s): %s",
                label, ran_for, proc.proc.returncode, (proc.tail[-1] if proc.tail else "no output"),
            )
            if not restarter.run_ended(ran_for, produced):
                return
            restarter.wait_before_restart(stop)
    finally:
        stop.set()
        if proc is not None:
            proc.stop()


def stream_encoded(spec: StreamSpec, label: str):
    """Yield one continuous encoded stream (FLAC/WAV).

    A restartable decoder feeds raw PCM to a single long-lived encoder, so a
    source restart never puts a second stream header in front of the player.
    """
    stop = threading.Event()
    restarter = Restarter(label)
    state: dict = {"decoder": None, "encoder": None}

    def start_decoder(pcm_rate: int | None) -> tuple[FFmpegProc, str] | None:
        while not stop.is_set():
            try:
                url, extra = resolve_source(spec.src)
            except Exception as e:
                log.warning("%s resolve failed: %s", label, e)
                if not restarter.run_ended(0, produced=False):
                    return None
                restarter.wait_before_restart(stop)
                continue
            cmd = decode_cmd(url, extra, bits=spec.bits, channels=spec.channels, rate=pcm_rate or spec.rate)
            return FFmpegProc(cmd, label, watch_output_rate=True), url
        return None

    # First decoder: learn the real output rate so the encoder can match it.
    while True:
        first = start_decoder(None)
        if first is None:
            return
        decoder, url = first
        state["decoder"] = decoder
        decoder.rate_known.wait(timeout=20)
        rate = spec.rate or decoder.output_rate
        if rate:
            break
        log.warning("%s could not determine sample rate: %s", label, decoder.tail[-3:])
        decoder.stop()
        if not restarter.run_ended(time.time() - decoder.started, produced=False):
            return
        restarter.wait_before_restart(stop)

    pcm = PcmFormat(rate=rate, channels=spec.channels, bits=spec.bits)
    log.info(
        "%s source %s (decode → %s %dHz %dch %d-bit)",
        label, _redact(url), spec.fmt, pcm.rate, pcm.channels, pcm.bits,
    )
    encoder = FFmpegProc(encode_cmd(pcm, spec.fmt), f"{label} encoder", stdin=subprocess.PIPE)
    state["encoder"] = encoder

    def pump() -> None:
        """Move PCM from whichever decoder is current into the encoder."""
        dec = state["decoder"]
        try:
            while not stop.is_set():
                produced = False
                while not stop.is_set():
                    data = dec.stdout.read(CHUNK)
                    if not data:
                        break
                    produced = True
                    encoder.proc.stdin.write(data)

                if stop.is_set():
                    return
                dec.stop()
                ran_for = time.time() - dec.started
                log.warning(
                    "%s decoder ended after %.0fs (exit=%s): %s",
                    label, ran_for, dec.proc.returncode, (dec.tail[-1] if dec.tail else "no output"),
                )
                if not restarter.run_ended(ran_for, produced):
                    return
                restarter.wait_before_restart(stop)
                # Later decoders are pinned to the encoder's format.
                nxt = start_decoder(pcm.rate)
                if nxt is None:
                    return
                dec = nxt[0]
                state["decoder"] = dec
                log.info("%s source %s (decoder restarted)", label, _redact(nxt[1]))
        except (BrokenPipeError, ValueError, OSError):
            pass  # encoder gone: the player disconnected
        finally:
            with contextlib.suppress(Exception):
                encoder.proc.stdin.close()

    threading.Thread(target=pump, daemon=True, name=f"pump:{label}").start()

    try:
        while True:
            chunk = encoder.stdout.read(CHUNK)
            if not chunk:
                break
            yield chunk
    finally:
        stop.set()
        for p in (state["decoder"], state["encoder"]):
            if p is not None:
                p.stop()


# ---------- what each listener was sent ----------
# Bookkeeping, kept so that a stop can be examined afterwards: for each listener
# the last few minutes of audio exactly as sent, a timeline, and every log line
# about its connection. Shown live at /status, and written to CAPTURE_DIR when
# the listener leaves. Same layout and numbers as radioproxy's listener.py.
#
# None of it decides what is sent or when. It runs after each piece has gone,
# and a fault in it is logged once and switched off for that listener, never
# passed on to the stream.
CAPTURE_DIR = os.environ.get("CAPTURE_DIR", "/data/captures")
CAPTURE_SECONDS = 300.0           # how much recent audio to keep per listener
CAPTURE_MAX_BYTES = 32 * 1024 * 1024
KEEP_CAPTURES = 12                # oldest captures are deleted beyond this
ROW_EVERY_S = 10.0                # one timeline row per this long
PAUSE_NOTE_S = 2.0                # a wait this long for the source's next audio is noted
BLOCKED_OVER_S = 0.02             # a write slower than this was waiting for the listener to take data
EVENTS_KEPT = 200
HEAD_KEEP = 64 * 1024             # opening bytes kept for the formats whose header a player needs
_EXTENSIONS = {"adts": "aac", "mp4": "mp4", "mpegts": "ts", "wav": "wav", "flac": "flac", "mp3": "mp3"}
_CAPTURE_NAME_RE = re.compile(r"^\d{8}-\d{6}-.+\.txt$")
_ADTS_RATES = (96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050, 16000, 12000, 11025, 8000, 7350)
_AAC_PROFILES = ("Main", "LC", "SSR", "LTP")
_MP3_BYTES_PER_S = int(MP3_BITRATE.rstrip("k")) * 1000 / 8
_STARTED = time.monotonic()


def _build_date() -> str:
    try:
        with open("/app/BUILD_DATE") as f:
            return f.read().strip() or "unknown"
    except OSError:
        return "unknown"


def _clock(t: float, day_of: float | None = None) -> str:
    """Time of day, with the date in front when it is not the same day as day_of."""
    when = time.localtime(t)
    if day_of is not None and when[:3] != time.localtime(day_of)[:3]:
        return time.strftime("%d %b %H:%M:%S", when)
    return time.strftime("%H:%M:%S", when)


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._") or "stream"


def _aac(fmt: tuple[int, int, int]) -> str:
    profile, rate, channels = fmt
    return f"AAC {_AAC_PROFILES[profile]} {rate} Hz {({0: '?', 7: 8}).get(channels, channels)} ch"


def _adts_scan(data: bytes) -> tuple[float, tuple[int, int, int] | None]:
    """Seconds of audio in a run of whole ADTS frames, and the (profile, rate, channels) of the last.

    Raises ValueError if the data is anything else.
    """
    i, n, seconds, fmt = 0, len(data), 0.0, None
    while i < n:
        if n - i < AdtsFramer.HEADER or data[i] != 0xFF or (data[i + 1] & 0xF6) != 0xF0:
            raise ValueError("not an ADTS frame")
        rate_index = (data[i + 2] >> 2) & 0x0F
        length = ((data[i + 3] & 0x03) << 11) | (data[i + 4] << 3) | (data[i + 5] >> 5)
        if rate_index > 12 or length < AdtsFramer.HEADER or i + length > n:
            raise ValueError("not an ADTS frame")
        rate = _ADTS_RATES[rate_index]
        seconds += ((data[i + 6] & 0x03) + 1) * 1024 / rate
        fmt = (data[i + 2] >> 6, rate, ((data[i + 2] & 0x01) << 2) | (data[i + 3] >> 6))
        i += length
    return seconds, fmt


def _wav_layout(head: bytes) -> tuple[int, int, int] | None:
    """(header length, bytes per sample frame, bytes per second) from the opening bytes of a WAV stream."""
    if head[:4] != b"RIFF" or head[8:12] != b"WAVE":
        return None
    i, frame, per_second = 12, 0, 0
    while i + 8 <= len(head):
        kind, size = head[i:i + 4], int.from_bytes(head[i + 4:i + 8], "little")
        if kind == b"fmt " and i + 22 <= len(head):
            per_second = int.from_bytes(head[i + 16:i + 20], "little")
            frame = int.from_bytes(head[i + 20:i + 22], "little")
        elif kind == b"data":
            return (i + 8, frame, per_second) if frame and per_second else None
        i += 8 + size + (size & 1)
    return None


def _flac_head_len(head: bytes) -> int | None:
    """Length of a FLAC stream's opening metadata: everything before the first audio frame."""
    if head[:4] != b"fLaC":
        return None
    i = 4
    while i + 4 <= len(head):
        last, size = head[i] & 0x80, int.from_bytes(head[i + 1:i + 4], "big")
        i += 4 + size
        if last:
            return i if i <= len(head) else None
    return None


def _drop(pieces: list[bytes], count: int) -> list[bytes]:
    """The pieces without their first `count` bytes."""
    out = list(pieces)
    while count > 0 and out:
        if len(out[0]) > count:
            out[0] = out[0][count:]
            break
        count -= len(out.pop(0))
    return out


@dataclass
class Row:
    """One line of a listener's timeline."""
    at: float                  # wall clock when the row was opened
    opened: float              # the same moment on the monotonic clock
    size: int = 0
    audio_s: float | None = None
    pause_s: float = 0.0       # longest wait for the source's next piece
    blocked_s: float = 0.0     # spent waiting for the listener to take data
    unsent: int | None = None
    rtt_ms: float | None = None
    retransmits: int | None = None


_save_lock = threading.Lock()


class Listener:
    def __init__(self, label: str, spec: StreamSpec, peer: str, sock) -> None:
        self.label = label
        self.station = spec.name
        self.peer = peer
        self.fmt = spec.fmt
        self._sock = sock
        self.started = time.time()
        self._started_mono = time.monotonic()
        self._lock = threading.Lock()
        self.sent = 0
        # Counted from the AAC frames themselves, or worked out from the bytes (MP3, WAV).
        # Not counted for FLAC, MP4 or MPEG-TS.
        self.audio_s: float | None = 0.0 if spec.fmt in ("adts", "mp3", "wav") else None
        self.blocked_s = 0.0
        self.write_began: float | None = None    # set by the stream loop while a write is under way
        self._first_at: float | None = None      # monotonic: the first audio handed over
        self._last_at: float | None = None       # monotonic: the latest piece handed over
        self._format: tuple[int, int, int] | None = None
        self._wav: tuple[int, int, int] | None = None
        self._keep_head = spec.fmt in ("wav", "flac")
        self._head = bytearray()
        self._audio: collections.deque[tuple[float, int, bytes]] = collections.deque()
        self._audio_bytes = 0
        self.rows: collections.deque[Row] = collections.deque(maxlen=400)
        self._row: Row | None = None
        self.events: collections.deque[tuple[float, str]] = collections.deque(maxlen=EVENTS_KEPT)
        self.source: str | None = None
        self.source_restarts = 0
        self._broken = False
        self._ended: tuple[float, float, str] | None = None   # (wall clock, monotonic, reason)
        self._finished = False

    # ---- while streaming ----

    def note(self, text: str) -> None:
        """Something notable. The log line comes back through _EventTap, which keeps it for the report."""
        log.warning("%s %s", self.label, text)

    def event(self, at: float, text: str) -> None:
        """A log line about this connection, filed here by _EventTap."""
        if text.startswith("source "):
            self.source = text[len("source "):]
        elif text.startswith("restarting source"):
            self.source_restarts += 1
        self.events.append((at, text))

    def record(self, data: bytes, began: float) -> None:
        """One piece of audio has been handed to the listener's connection; that write began at `began`."""
        self.write_began = None
        if self._broken:
            return
        try:
            self._record(data, began, time.monotonic())
        except Exception:
            self._broken = True
            log.exception(
                "%s bookkeeping failed and is off for this listener; the stream itself is unaffected", self.label
            )

    def _record(self, data: bytes, began: float, now: float) -> None:
        took = now - began
        blocked = took if took >= BLOCKED_OVER_S else 0.0
        before = self._last_at
        pause = 0.0 if before is None else max(0.0, began - before)
        offset = self.sent
        if self._keep_head and len(self._head) < HEAD_KEEP:
            self._head += data[:HEAD_KEEP - len(self._head)]
        audio_s = self._seconds(data, offset)
        with self._lock:
            if self._first_at is None:
                self._first_at = began
            self._last_at = now
            self.sent = offset + len(data)
            self.blocked_s += blocked
            if audio_s is not None and self.audio_s is not None:
                self.audio_s += audio_s
            self._audio.append((now, offset, data))
            self._audio_bytes += len(data)
            horizon = now - CAPTURE_SECONDS
            while self._audio and (self._audio[0][0] < horizon or self._audio_bytes > CAPTURE_MAX_BYTES):
                self._audio_bytes -= len(self._audio.popleft()[2])
            row = self._row
            if row is None:
                row = self._row = Row(time.time(), now, audio_s=None if self.audio_s is None else 0.0)
            row.size += len(data)
            row.blocked_s += blocked
            row.pause_s = max(row.pause_s, pause)
            if audio_s is not None and row.audio_s is not None:
                row.audio_s += audio_s
            full = now - row.opened >= ROW_EVERY_S
            if full:
                self._row = None
        if full:
            row.unsent, row.rtt_ms, row.retransmits = self._tcp()
            self.rows.append(row)
        if pause >= PAUSE_NOTE_S:
            self.note(f"no audio from the source for {pause:.1f}s (since {_clock(time.time() - (now - before))})")

    def _seconds(self, data: bytes, offset: int) -> float | None:
        """Seconds of audio in this piece, for the formats where that can be told."""
        if self.audio_s is None:
            return None
        if self.fmt == "adts":
            try:
                seconds, fmt = _adts_scan(data)
            except ValueError:
                self.audio_s = None
                self.note("the stream stopped being whole AAC frames; seconds of audio no longer counted")
                return None
            if fmt != self._format:
                if self._format is not None:
                    self.note(f"audio format changed mid-stream: {_aac(self._format)} -> {_aac(fmt)}")
                self._format = fmt
            return seconds
        if self.fmt == "mp3":
            return len(data) / _MP3_BYTES_PER_S
        # WAV: its header says how many bytes make a second.
        if self._wav is None:
            self._wav = _wav_layout(self._head)
            if self._wav is None:
                if len(self._head) >= HEAD_KEEP:
                    self.audio_s = None   # no header where one should be
                return None
        head_len, _, per_second = self._wav
        return max(0, offset + len(data) - max(offset, head_len)) / per_second

    def _tcp(self) -> tuple[int | None, float | None, int | None]:
        """From the listener's connection: bytes it has not yet accepted, round trip (ms), total retransmits."""
        try:
            fd = self._sock.fileno()
            unsent = struct.unpack("i", fcntl.ioctl(fd, termios.TIOCOUTQ, b"\0\0\0\0"))[0]
            info = struct.unpack("8B24I", self._sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_INFO, 104))
            return unsent, info[8 + 15] / 1000.0, info[8 + 23]
        except (OSError, AttributeError, struct.error, ValueError):
            return None, None, None

    # ---- status ----

    def _now(self) -> float:
        return self._ended[1] if self._ended else time.monotonic()

    def lead_s(self) -> float | None:
        """Audio sent beyond real time since the first of it went: roughly what the player is holding."""
        if self.audio_s is None or self._first_at is None:
            return None
        return self.audio_s - (self._now() - self._first_at)

    @property
    def description(self) -> str:
        parts = [("copy" if self.fmt in COPY_FORMATS else "decode") + f" → {self.fmt}"]
        if self._format is not None:
            parts.append(_aac(self._format))
        parts.append(f"{BURST_SECONDS:g}s burst then real time")
        return ", ".join(parts)

    def status(self) -> dict:
        unsent, rtt_ms, retransmits = self._tcp()
        now = time.monotonic()
        lead = self.lead_s()
        blocked, writing = self.blocked_s, self.write_began
        if writing is not None and now - writing >= BLOCKED_OVER_S:
            blocked += now - writing   # a write that has not returned yet
        return {
            "station": self.station,
            "listener": self.peer,
            "stream": self.description,
            "connected_at": self.started,
            "connected_s": round(now - self._started_mono),
            "sent_mb": round(self.sent / 1e6, 1),
            "audio_sent_s": None if self.audio_s is None else round(self.audio_s),
            "listener_holding_s": None if lead is None else round(lead),
            "not_yet_accepted_bytes": unsent,
            "round_trip_ms": rtt_ms,
            "retransmits": retransmits,
            "blocked_s": round(blocked, 1),
            "events": [f"{_clock(at)} {text}" for at, text in list(self.events)[-20:]],
            # The fields above are radioproxy's; these are the restreamer's own.
            "fmt": self.fmt,
            "source": self.source,
            "source_restarts": self.source_restarts,
            "since_last_audio_s": None if self._last_at is None else round(now - self._last_at, 1),
        }

    # ---- when the listener leaves ----

    def stopped(self, reason: str) -> None:
        """The stream to this listener has just ended: note the moment. finish() does the rest."""
        self.write_began = None
        if self._ended is None:
            self._ended = (time.time(), time.monotonic(), reason)

    def finish(self) -> None:
        """Log how it went, and write the capture and report. Once only."""
        with self._lock:
            if self._finished:
                return
            self._finished = True
        if self._ended is None:
            self.stopped("ended")
        reason = self._ended[2]
        try:
            saved = self.save()
            log.info(
                "%s disconnected (%s) after %s%s",
                self.label, reason, self.summary(), f"; capture {saved}" if saved else "",
            )
        except Exception:
            log.exception("%s could not write its report", self.label)
            log.info("%s disconnected (%s) after %.0fs", self.label, reason, self._ended[1] - self._started_mono)

    def summary(self) -> str:
        unsent, rtt_ms, retransmits = self._tcp()
        parts = [f"{self._now() - self._started_mono:.0f}s", f"{self.sent / 1e6:.1f} MB sent"]
        lead = self.lead_s()
        if lead is not None:
            parts.append(f"{self.audio_s:.0f}s of audio (listener holding ~{lead:.0f}s)")
        if unsent is not None:
            parts.append(f"{unsent} B not yet accepted, round trip {rtt_ms:.0f} ms, {retransmits} retransmits")
        return ", ".join(parts)

    def save(self) -> str | None:
        """Write the recent audio and a report.

        Returns the audio file's path (the report's, if no audio was sent), or
        None if there is nowhere to write.
        """
        with self._lock:
            entries = list(self._audio)
            head = bytes(self._head)
            rows = list(self.rows)
            unfinished = self._row
        if unfinished is not None:   # the row that was still filling: close it with a reading taken now
            unsent, rtt_ms, retransmits = self._tcp()
            rows.append(replace(unfinished, unsent=unsent, rtt_ms=rtt_ms, retransmits=retransmits))
        pieces, how = self._playable(entries, head)
        try:
            with _save_lock:
                os.makedirs(CAPTURE_DIR, exist_ok=True)
                stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(self._ended[0]))
                stem = base = os.path.join(CAPTURE_DIR, f"{stamp}-{_safe_name(self.station)}")
                extra = 1
                while os.path.exists(f"{stem}.txt"):   # two listeners of one station leaving in the same second
                    extra += 1
                    stem = f"{base}-{extra}"
                audio_path = None
                if pieces:
                    audio_path = f"{stem}.{_EXTENSIONS.get(self.fmt, 'bin')}"
                    with open(audio_path, "wb") as f:
                        for data in pieces:
                            f.write(data)
                with open(f"{stem}.txt", "w", encoding="utf-8") as f:
                    f.write(self._report(rows, entries, audio_path and os.path.basename(audio_path), how))
                _prune_captures()
            return audio_path or f"{stem}.txt"
        except OSError as exc:
            log.info("%s capture not saved (%s)", self.label, exc)
            return None

    def _playable(self, entries: list[tuple[float, int, bytes]], head: bytes) -> tuple[list[bytes], str]:
        """The kept audio in a form a player can open, and a few words on what was done to it.

        AAC, MP3 and MPEG-TS can be cut anywhere. WAV and FLAC cannot be read
        without the header from the very start of the stream, so once that has
        dropped out of the kept audio it is put back in front (and WAV is cut
        on a whole sample).
        """
        pieces = [data for _, _, data in entries]
        as_sent = "exactly as the listener received it"
        if not entries or entries[0][1] == 0:
            return pieces, as_sent
        start = entries[0][1]
        with_head = "as the listener received it, with the stream's opening header put back in front"
        if self.fmt == "wav":
            layout = _wav_layout(head)
            if layout:
                head_len, frame, _ = layout
                first = max(start, head_len)
                first += -(first - head_len) % frame
                return [head[:head_len], *_drop(pieces, first - start)], with_head
        elif self.fmt == "flac":
            head_len = _flac_head_len(head)
            if head_len:
                return [head[:head_len], *_drop(pieces, max(0, head_len - start))], with_head
        return pieces, as_sent

    def _report(self, rows: list[Row], entries: list[tuple[float, int, bytes]], audio_name: str | None, how: str) -> str:
        ended_wall, ended_mono, reason = self._ended
        unsent, rtt_ms, retransmits = self._tcp()
        lead = self.lead_s()

        def stamp(t: float) -> str:
            return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))

        restarts = self.source_restarts
        lines = [
            f"station:       {self.station}  ({self.description})",
            f"source:        {self.source or 'never opened'}"
            + (f"; restarted {restarts} time{'' if restarts == 1 else 's'}" if restarts else ""),
            f"listener:      {self.peer}",
            f"connected:     {stamp(self.started)}",
            f"disconnected:  {stamp(ended_wall)}  ({reason}) after {ended_mono - self._started_mono:.0f} s",
            f"sent:          {self.sent / 1e6:.1f} MB"
            + ("" if self.audio_s is None else f" = {self.audio_s:.0f} s of audio"),
        ]
        if lead is not None and lead >= 0:
            lines.append(
                f"player buffer: about {lead:.0f} s (audio sent beyond real time). If the player was playing"
                f" normally, it was about {lead:.0f} s before the end of everything sent."
            )
        elif lead is not None:
            lines.append(
                f"player buffer: none. {-lead:.0f} s less audio was sent than time passed, so a player that"
                " kept playing had run dry."
            )
        if unsent is not None:
            lines.append(
                f"connection:    {unsent} B sent but not yet accepted by the listener, round trip {rtt_ms:.0f} ms,"
                f" {retransmits} packets retransmitted in total, {self.blocked_s:.1f} s spent waiting for it to take data"
            )
        else:
            lines.append(f"connection:    {self.blocked_s:.1f} s spent waiting for the listener to take data")
        if self._last_at is None:
            lines.append("last audio:    none was sent")
        elif ended_mono - self._last_at >= 1.0:
            quiet = ended_mono - self._last_at
            lines.append(
                f"last audio:    {quiet:.1f} s before the end. The restreamer only finds a listener gone when it"
                f" next sends, so the listener may have left at any point in those {quiet:.1f} s."
            )
        if audio_name:
            kept = sum(len(data) for _, _, data in entries)
            lines.append(
                f"audio file:    {audio_name}: the last {kept / 1e6:.1f} MB sent"
                f" (pieces handed over in the final {ended_mono - entries[0][0]:.0f} s), {how}"
            )
        else:
            lines.append("audio file:    none")
        if self._broken:
            lines.append("note:          the bookkeeping failed part-way (see events); the figures stop there")
        lines += [
            "",
            "events:",
            *([f"  {_clock(at, ended_wall)}  {text}" for at, text in list(self.events)] or ["  none"]),
            "",
            f"timeline (most recent last, one row per {ROW_EVERY_S:g} s):",
            "  time      audio   bytes     longest pause  waited   not-accepted  rtt     retrans",
        ]
        for r in rows[-60:]:
            lines.append(
                f"  {_clock(r.at)}  {'' if r.audio_s is None else format(r.audio_s, '.1f') + 's':<6}  {r.size:<8}  "
                f"{format(r.pause_s, '.2f') + 's':<13}  {r.blocked_s:<6.2f}  "
                f"{'' if r.unsent is None else r.unsent:<12}  {'' if r.rtt_ms is None else format(r.rtt_ms, '.0f') + 'ms':<6}  "
                f"{'' if r.retransmits is None else r.retransmits}"
            )
        return "\n".join(lines) + "\n"


def _prune_captures() -> None:
    """Keep the newest KEEP_CAPTURES reports and their audio. Only ever touches files named as save() names them."""
    reports = sorted(f for f in os.listdir(CAPTURE_DIR) if _CAPTURE_NAME_RE.match(f))
    for old in reports[:-KEEP_CAPTURES]:
        stem = old[:-4]
        for name in os.listdir(CAPTURE_DIR):
            if name.startswith(stem + "."):
                with contextlib.suppress(FileNotFoundError):
                    os.remove(os.path.join(CAPTURE_DIR, name))


def _captures_note() -> str:
    """One line for the start-up log on whether captures can be written, and whether they will last."""
    try:
        os.makedirs(CAPTURE_DIR, exist_ok=True)
        probe = os.path.join(CAPTURE_DIR, ".write-test")
        with open(probe, "w"):
            pass
        os.remove(probe)
    except OSError as exc:
        return f"captures off: cannot write to {CAPTURE_DIR} ({exc.strerror or exc})"
    kept = f"captures in {CAPTURE_DIR}: the last {CAPTURE_SECONDS:g}s of audio and a report per listener, {KEEP_CAPTURES} kept"
    if os.path.ismount(CAPTURE_DIR):
        return kept
    return kept + " (not a mounted folder, so they go when the container is replaced)"


_listeners: dict[int, Listener] = {}
# Never log while holding this: _EventTap takes it from inside the logging call.
_listeners_lock = threading.Lock()


class _EventTap(logging.Handler):
    """Files each log line about a connection under its listener, for /status and the report.

    The stream code already logs what happens to a source (opened, ended,
    restarted, ffmpeg's own warnings), and every such line starts with the
    connection's label. Picking them up here leaves that code as it was.
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = record.getMessage()
            with _listeners_lock:
                listeners = list(_listeners.values())
            for listener in listeners:
                if text.startswith(listener.label):
                    listener.event(record.created, text[len(listener.label):].strip())
                    return
        except Exception:
            self.handleError(record)


log.addHandler(_EventTap(level=logging.INFO))


def _status() -> dict:
    with _listeners_lock:
        listeners = list(_listeners.values())

    def one(listener: Listener) -> dict:
        try:
            return listener.status()
        except Exception as exc:
            return {"station": listener.station, "listener": listener.peer, "error": repr(exc)}

    return {
        "service": "restreamer",
        "built": _build_date(),
        "uptime_s": round(time.monotonic() - _STARTED),
        "default_fmt": DEFAULT_FMT,
        "burst_s": BURST_SECONDS,
        "captures": CAPTURE_DIR,
        "listeners": [one(x) for x in listeners],
    }


# ---------- HTTP ----------
class BadRequest(ValueError):
    """A request we refuse with 400."""


def _spec_from(name: str, src: str, opts: dict) -> StreamSpec:
    fmt = str(opts.get("fmt") or DEFAULT_FMT).lower()
    if fmt not in COPY_FORMATS | ENCODED_FORMATS:
        raise BadRequest(f"unsupported fmt {fmt!r}")

    def as_int(key: str, default, allowed=None):
        raw = opts.get(key)
        if raw in (None, ""):
            return default
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise BadRequest(f"{key} must be a number")
        if allowed and value not in allowed:
            raise BadRequest(f"{key} must be one of {sorted(allowed)}")
        return value

    return StreamSpec(
        name=name,
        src=src,
        fmt=fmt,
        bits=as_int("bits", 16, {16, 24}),
        channels=as_int("channels", 2, {1, 2}),
        rate=as_int("rate", None),
    )


_conn_ids = iter(range(1, 1 << 62))
_conn_lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    """Answers the way a plain Icecast server does.

    HTTP/1.0, no Content-Length, no chunked encoding: the body is the stream
    itself and ends when the connection closes.
    """

    protocol_version = "HTTP/1.0"

    def version_string(self) -> str:
        return "restreamer"

    def do_GET(self) -> None:
        url = urlparse(self.path)
        try:
            if url.path == "/health":
                return self._send_json(200, {"ok": True, "stations": len(STATIONS)})

            if url.path == "/status":
                return self._send_json(200, _status(), pretty=True)

            if url.path.startswith("/s/"):
                name = unquote(url.path[len("/s/"):])
                station = STATIONS.get(name)
                if not station or not station.get("url"):
                    return self._send_json(404, {"error": f"unknown station {name!r}"})
                return self._stream(_spec_from(name, str(station["url"]), station))

            if url.path == "/play":
                query = {k: v[-1] for k, v in parse_qs(url.query).items()}
                src = (query.get("src") or "").strip()
                if not src:
                    raise BadRequest("src is required (a URL or TuneIn station ID)")
                name = tunein_station_id(src) or urlparse(src).hostname or "stream"
                return self._stream(_spec_from(name, src, query))

            self._send_json(404, {"error": "not found"})
        except BadRequest as e:
            self._send_json(400, {"error": str(e)})

    def _send_json(self, status: int, body: dict, *, pretty: bool = False) -> None:
        if pretty:   # for people: indented, and arrows and accents left readable
            data = (json.dumps(body, indent=2, ensure_ascii=False) + "\n").encode()
        else:
            data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8" if pretty else "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _stream(self, spec: StreamSpec) -> None:
        client = self.headers.get("X-Forwarded-For") or self.client_address[0]
        with _conn_lock:
            conn = next(_conn_ids)
        label = f"[{spec.name} #{conn} ← {client}]"
        log.info("%s connected fmt=%s", label, spec.fmt)

        self.send_response(200)
        self.send_header("Content-Type", CTYPES[spec.fmt])
        self.send_header("Cache-Control", "no-cache, no-store")
        self.send_header("Pragma", "no-cache")
        self.send_header("icy-name", spec.name)
        self.send_header("icy-pub", "0")
        self.end_headers()

        gen = stream_copy(spec, label) if spec.fmt in COPY_FORMATS else stream_encoded(spec, label)
        # The listener object only watches: see "what each listener was sent" above.
        listener = Listener(label, spec, client, self.connection)
        with _listeners_lock:
            _listeners[conn] = listener
        reason = "stream failed"
        try:
            for chunk in gen:
                began = listener.write_began = time.monotonic()
                self.wfile.write(chunk)
                listener.record(chunk, began)
            reason = "source ended"
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            reason = "listener hung up"  # the player went away
        finally:
            listener.stopped(reason)
            gen.close()
            with _listeners_lock:
                _listeners.pop(conn, None)
            listener.finish()

    def log_message(self, fmt: str, *args) -> None:
        # Streams log their own connect/disconnect; skip the per-minute healthcheck.
        if not self.path.startswith("/health"):
            log.debug("%s %s", self.address_string(), fmt % args)


class Server(ThreadingHTTPServer):
    daemon_threads = True  # don't hold shutdown for open streams
    allow_reuse_address = True


def main() -> None:
    port = int(os.environ.get("PORT", "8000"))
    server = Server(("0.0.0.0", port), Handler)

    def stop(*_):
        # shutdown() waits for serve_forever, so it must run off the main thread.
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    log.info(
        "listening on :%d (built %s, default fmt %s, %gs burst then real time, %d stations)",
        port, _build_date(), DEFAULT_FMT, BURST_SECONDS, len(STATIONS),
    )
    log.info(_captures_note())
    server.serve_forever()
    # Stopping cuts every listener off: leave a report for each, saying it was us.
    with _listeners_lock:
        remaining = list(_listeners.values())
    for listener in remaining:
        listener.stopped("restreamer stopped")
        listener.finish()
    server.server_close()


if __name__ == "__main__":
    main()
