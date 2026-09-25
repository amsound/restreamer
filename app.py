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
  GET /health                        liveness
"""

import contextlib
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
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
ENCODED_FORMATS = {"flac", "wav"}

# MIME types for output formats
CTYPES = {
    "mp4": "audio/mp4",
    "mpegts": "video/MP2T",
    "adts": "audio/aac",
    "wav": "audio/wav",
    "flac": "audio/flac",
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


def encode_cmd(pcm: PcmFormat, fmt: str) -> list[str]:
    """Encoder: raw PCM on stdin → one continuous output stream. Lives for the whole session."""
    base = [
        "ffmpeg", "-hide_banner", "-nostats", "-loglevel", "level+warning",
        "-f", pcm.ffmpeg_fmt, "-ar", str(pcm.rate), "-ac", str(pcm.channels), "-i", "pipe:0",
    ]

    if fmt == "wav":
        codec = "pcm_s16le" if pcm.bits == 16 else "pcm_s24le"
        return base + ["-c:a", codec, "-flush_packets", "1", "-f", "wav", "-"]

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


def stream_copy(spec: StreamSpec, label: str):
    """Yield the source's own audio, restarting ffmpeg in place when it exits."""
    stop = threading.Event()
    restarter = Restarter(label)
    proc: FFmpegProc | None = None
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
                yield chunk

            proc.stop()
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

    def _send_json(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _stream(self, spec: StreamSpec) -> None:
        client = self.headers.get("X-Forwarded-For") or self.client_address[0]
        with _conn_lock:
            conn = next(_conn_ids)
        label = f"[{spec.name} #{conn} ← {client}]"
        log.info("%s connected fmt=%s", label, spec.fmt)
        started = time.time()

        self.send_response(200)
        self.send_header("Content-Type", CTYPES[spec.fmt])
        self.send_header("Cache-Control", "no-cache, no-store")
        self.send_header("Pragma", "no-cache")
        self.send_header("icy-name", spec.name)
        self.send_header("icy-pub", "0")
        self.end_headers()

        gen = stream_copy(spec, label) if spec.fmt in COPY_FORMATS else stream_encoded(spec, label)
        try:
            for chunk in gen:
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            pass  # the player went away
        finally:
            gen.close()
            log.info("%s disconnected after %.0fs", label, time.time() - started)

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
        "listening on :%d (default fmt %s, %gs burst then real time, %d stations)",
        port, DEFAULT_FMT, BURST_SECONDS, len(STATIONS),
    )
    server.serve_forever()
    server.server_close()


if __name__ == "__main__":
    main()
