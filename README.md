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
| `/status` | Who is listening right now and how it is going (JSON) |
| `/health` | Liveness check (used by the Docker healthcheck) |

`src` can be a stream URL, `tunein:s345724`, a bare `s345724`, or a
`tunein.com` station URL. URL-encode it when it contains `?` or `&`.

```bash
curl -v http://localhost:8000/s/apple_music_hits > /dev/null
curl -v "http://localhost:8000/play?src=tunein:s345724&fmt=flac" > /dev/null
```

## Status and captures

Both work as they do in radioproxy, so a stop can be examined afterwards whichever
of the two was playing. None of it decides what is sent or when: it only watches,
and a fault in it is logged once and switched off for that listener.

`/status` lists the listeners. For each:

| Field | Meaning |
|---|---|
| `audio_sent_s`, `listener_holding_s` | Audio sent, and how much of it is beyond real time: roughly what the player is holding. Counted for `adts`, `mp3` and `wav`; `null` for `flac`, `mp4` and `mpegts`. |
| `not_yet_accepted_bytes`, `round_trip_ms`, `retransmits` | Read from the listener's own TCP connection. |
| `blocked_s` | Time spent waiting for the listener to take data, including a wait still going on. |
| `since_last_audio_s` | How long since audio last went out. More than a second or two means the source has gone quiet or the listener has stopped reading. |
| `source`, `source_restarts`, `events` | The source in use, how many times it has been restarted, and the latest log lines about this connection. |

When a listener leaves, the last five minutes of audio and a plain-text report
are written to `CAPTURE_DIR`, and the newest 12 are kept. The report holds the
same figures as the `disconnected` log line, every log line about the
connection, and a timeline with one row per 10 seconds: audio and bytes sent,
the longest wait for the source's next audio, time spent waiting for the
listener, and the TCP readings.

The audio file is exactly the bytes the listener was sent. WAV and FLAC cannot
be opened without the header from the start of the stream, so for those it is
put back in front.

Mount a folder at `/data/captures` to keep them when the image is updated. The
container runs as user 10001, so the folder has to belong to it:

```bash
mkdir -p restreamer-data && sudo chown 10001:10001 restreamer-data
```

The start-up log says where captures are going, or why they are off.

The restreamer only finds out that a listener has gone when it next sends to
it. While the source is quiet that can be late; the report then says how long
nothing had been sent.

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
| `CAPTURE_DIR` | `/data/captures` | Where each listener's last audio and report are written |
| `LOG_LEVEL` | `INFO` | |

## Logs

Each connection gets an ID, and the log shows the resolved source (signatures
stripped), the output format, every restart with ffmpeg's reason, any pause in
the source of 2 seconds or more, and the disconnect with what was sent:

```
[apple_music_hits #1 ← 192.168.70.51] connected fmt=adts
[apple_music_hits #1 ← 192.168.70.51] source https://itsliveradio.apple.com/.../usw/256.m3u8 (copy → adts)
[apple_music_hits #1 ← 192.168.70.51] no audio from the source for 8.1s (since 22:36:00)
[apple_music_hits #1 ← 192.168.70.51] disconnected (listener hung up) after 200s, 6.8 MB sent, 212s of audio (listener holding ~15s), 677 B not yet accepted, round trip 1 ms, 0 retransmits; capture /data/captures/20261007-064058-apple_music_hits.aac
```

The reason in brackets is `listener hung up`, `source ended` (the restreamer gave
up on the source) or `restreamer stopped`.

## hls_best_audio.sh

A standalone debugging tool, also available inside the container: saves the
best audio variant of an HLS stream to a file.

```bash
docker compose exec hls2aac ./hls_best_audio.sh "<master.m3u8>" /tmp/out.aac
```
