import os, signal, subprocess, contextlib, yaml, time, threading, re
from urllib.parse import urlparse
import requests
from flask import Flask, Response, abort

app = Flask(__name__)

# Load station definitions
with open(os.environ.get("STATIONS_FILE", "stations.yml"), "r") as f:
    STATIONS = yaml.safe_load(f) or {}

# MIME types for output formats
CTYPES = {"mp4": "audio/mp4", "mpegts": "video/MP2T", "adts": "audio/aac", "wav": "audio/wav", "flac": "audio/flac"}

UA = os.environ.get("UA", "VLC/3.0")
HTTP_TIMEOUT = (3, 10)  # (connect, read) seconds
HLS_RE = re.compile(r"\.m3u8($|\?)", re.I)
HDNEA_RE = re.compile(r"(?i)\bhdnea=[^;]+")

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
    if re.match(r"^https?://", rel): return rel
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

    # M3U8 (master/media) → we’ll handle in choose_hls_best later, but resolving is safe
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

    extra = ["-user_agent", UA]
    if cookie:
        extra += ["-headers", f"Cookie: {cookie}"]
    return in_url, extra

# ---------- ffmpeg command builder (now accepts extra headers) ----------
def ffmpeg_cmd(
    url: str, fmt: str,
    bits: str = "", rate: str = "", ch: str = "",
    extra_headers: list[str] | None = None
):
    extra_headers = extra_headers or []

    base = [
        "ffmpeg", "-loglevel", "warning", "-nostdin",
        "-user_agent", UA,
        "-headers", "Icy-MetaData: 1",
        *extra_headers,
        "-analyzeduration", "0", "-probesize", "32k",
        "-fflags", "+nobuffer",
        "-rw_timeout", "15000000",
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_on_network_error", "1",
        "-i", url,
    ]

    if fmt == "mp4":
        return base + [
            "-c:a", "copy", "-bsf:a", "aac_adtstoasc",
            "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
            "-muxdelay", "0", "-muxpreload", "0",
            "-f", "mp4", "-"
        ]

    if fmt == "mpegts":
        return base + [
            "-c:a", "copy",
            "-muxdelay", "0", "-muxpreload", "0",
            "-f", "mpegts", "-"
        ]

    if fmt == "adts":
        return base + [
            "-c:a", "copy",
            "-fflags", "+flush_packets", "-flush_packets", "1",
            "-muxdelay", "0", "-muxpreload", "0",
            "-f", "adts", "-"
        ]

    if fmt == "wav":
        pcm = "pcm_s16le" if bits == "16" else ("pcm_s24le" if bits == "24" else "pcm_s16le")
        args = ["-vn", "-sn", "-acodec", pcm]
        if ch:   args += ["-ac", ch]
        if rate: args += ["-ar", rate]
        return base + args + ["-f", "wav", "-"]

    if fmt == "flac":
        args = ["-vn", "-sn", "-c:a", "flac", "-compression_level", "5"]
        if bits in ("16", "24"):
            sfmt = "s16" if bits == "16" else "s24"
            args += ["-af", f"aformat=sample_fmts={sfmt}:channel_layouts=stereo",
                     "-sample_fmt", sfmt, "-bits_per_raw_sample", bits]
        if ch:   args += ["-ac", ch]
        if rate: args += ["-ar", rate]
        return base + args + ["-f", "flac", "-"]

    # fallback
    return base + ["-c:a", "copy", "-f", "adts", "-"]

def _drain_stderr(proc: subprocess.Popen):
    # keep ffmpeg's stderr pipe empty so it can't block under error spam
    try:
        for _ in iter(proc.stderr.readline, b""):
            pass
    except Exception:
        pass

@app.route("/s/<name>")
def serve(name: str):
    spec = STATIONS.get(name)
    if not spec:
        abort(404)

    # Optional per-station output controls (strings expected)
    fmt  = str(spec.get("fmt", "adts")).lower()
    bits = str(spec.get("bits", ""))       # "16" or "24" (only used for wav/flac)
    rate = str(spec.get("rate", ""))       # e.g. "44100" or "48000"
    ch   = str(spec.get("channels", ""))   # e.g. "2"

    # Always resolve the source freshly (handles redirects / playlists / session keys)
    src = spec["url"]
    try:
        resolved = resolve_once(src)
    except Exception as e:
        abort(502, description=f"Failed to resolve source: {e}")

    # If this is HLS, choose highest child + cookies; otherwise keep old behavior
    extra_headers = []
    in_url = resolved
    try:
        if HLS_RE.search(resolved):
            in_url, extra_headers = choose_hls_best(resolved)
    except Exception:
        # Fall back to the original resolved URL; still playable in many cases.
        in_url, extra_headers = resolved, []

    ctype = CTYPES.get(fmt, "application/octet-stream")
    cmd = ffmpeg_cmd(in_url, fmt, bits=bits, rate=rate, ch=ch, extra_headers=extra_headers)

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
    threading.Thread(target=_drain_stderr, args=(proc,), daemon=True).start()

    STALL_SEC = 60
    last = time.time()

    def stream():
        nonlocal last
        try:
            while True:
                chunk = proc.stdout.read(256 * 1024)
                if not chunk:
                    # ffmpeg exited or source stalled
                    if proc.poll() is not None:
                        break
                    if time.time() - last > STALL_SEC:
                        break
                    time.sleep(0.05)
                    continue
                last = time.time()
                yield chunk
        finally:
            with contextlib.suppress(Exception):
                proc.terminate()
            try:
                proc.wait(timeout=2)
            except Exception:
                with contextlib.suppress(Exception):
                    os.kill(proc.pid, signal.SIGKILL)

    headers = {"Cache-Control": "no-store, max-age=0", "Connection": "close"}
    return Response(stream(), mimetype=ctype, headers=headers)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
