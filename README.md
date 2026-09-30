# restreamer

Turns any radio source into one steady, never-ending HTTP stream in a format
you choose, so a speaker only ever sees one simple kind of stream.

The stream looks exactly like a plain Icecast server: an `HTTP/1.0` response
with a raw body (no `Content-Length`, no chunked encoding), a few seconds of
audio up front and then real time. Even HLS, which arrives a whole segment at a
time, reaches the player as a steady trickle. Basic players, such as the Cast
receiver in Samsung soundbars, cope with this best.

Sources: Icecast/Shoutcast, PLS and M3U playlists, HLS (picks the best variant),
and TuneIn stations (resolved fresh on every connect, so signed URLs never go stale).

If the source drops, a signed URL expires or ffmpeg exits, the restreamer
re-resolves the source and restarts ffmpeg **inside the same response**. The
player keeps its connection and at most hears a short gap.

## Build and run

GitHub Actions (`.github/workflows/image.yml`) builds the arm64 image on every push to `main` that touches
the code, and publishes it to `ghcr.io/amsound/restreamer`: `:latest`, plus `:sha-<commit>` for rolling back.

```bash
docker compose pull && docker compose up -d
```

Build locally instead with `docker build -t ghcr.io/amsound/restreamer:latest .`.

`stations.yaml` sits next to `docker-compose.yaml` and is mounted read-only.

## Endpoints

| URL | What it does |
|---|---|
| `/s/<name>` | A station from `stations.yaml` |
| `/play?src=<source>&fmt=<fmt>` | Any source, no config needed |
| `/health` | Liveness check (used by the Docker healthcheck) |

`src` can be a stream URL, `tunein:s345724`, a bare `s345724`, or a
`tunein.com` station URL. URL-encode it when it contains `?` or `&`.

```bash
curl -v http://localhost:8000/s/apple_music_hits > /dev/null
curl -v "http://localhost:8000/play?src=tunein:s345724&fmt=flac" > /dev/null
```

## Formats

| `fmt` | Output | Notes |
|---|---|---|
| `adts` | AAC, untouched | Codec, bitrate and sample rate passed straight through, whole frames only (a frame cut by a source restart is dropped). AAC sources only; an MP3 source is refused with a clear log line. |
| `flac` | FLAC | Decoded and re-encoded, so every station comes out the same. `bits: 16` or `24`. |
| `mp3` | MP3, 320 kbps CBR | Decoded and re-encoded like `flac`. The most widely supported format; the choice for fussy players. |
| `wav` | PCM WAV | As `flac`, uncompressed. |
| `mpegts`, `mp4` | AAC in TS / fragmented MP4 | Copy formats, as `adts`. |

`flac`, `mp3` and `wav` pass the source sample rate through (MP3: 32, 44.1 or 48 kHz, otherwise resampled to 48 kHz) unless you set `rate`, and
output stereo unless you set `channels: 1`. They run as two ffmpeg processes: a
restartable decoder feeding one long-lived encoder, so a source restart never
sends the player a second stream header.

## Settings

Per station (or as `/play` query parameters): `fmt`, `bits`, `rate`, `channels`.

Environment:

| Variable | Default | |
|---|---|---|
| `DEFAULT_FMT` | `adts` | Format when a station doesn't set one |
| `BURST_SECONDS` | `4` | Audio sent at once on connect; after that, real time. Raise it if a player needs a bigger buffer. |
| `UA` | `VLC/3.0` | User agent sent upstream |
| `STATIONS_FILE` | `/data/stations.yaml` | |
| `LOG_LEVEL` | `INFO` | |

## Logs

Each connection gets an ID, and the log shows the resolved source (signatures
stripped), the output format, every restart with ffmpeg's reason, and the
disconnect:

```
[apple_music_hits #1 ← 192.168.70.51] connected fmt=adts
[apple_music_hits #1 ← 192.168.70.51] source https://itsliveradio.apple.com/.../usw/256.m3u8 (copy → adts)
```

## hls_best_audio.sh

A standalone debugging tool, also available inside the container: saves the
best audio variant of an HLS stream to a file.

```bash
docker compose exec hls2aac ./hls_best_audio.sh "<master.m3u8>" /tmp/out.aac
```
