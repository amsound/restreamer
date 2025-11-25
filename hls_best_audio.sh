# File: hls_best_audio.sh
#!/usr/bin/env bash
set -Eeuo pipefail
URL="${1:?usage: hls_best_audio.sh <hls-master.m3u8> [out.aac]}"
OUT="${2:-out.aac}"
UA="${UA:-Mozilla/5.0}"
HDRS_CURL=(-H "User-Agent: $UA" --fail --silent --show-error --location)
[[ -n "${COOKIE:-}" ]] && HDRS_CURL+=(-H "Cookie: $COOKIE")
HDRS_FF=()
[[ -n "${COOKIE:-}" ]] && HDRS_FF+=(-headers "Cookie: $COOKIE")
HDRS_FF+=(-user_agent "$UA")
tmpjson="$(mktemp)"
trap 'rm -f "$tmpjson"' EXIT
if ffprobe -v error "${HDRS_FF[@]}" -print_format json -show_programs -show_streams -i "$URL" >"$tmpjson"; then
  if [[ -s "$tmpjson" ]]; then
    MAP_SEL="$(
      python3 - <<'PY' "$tmpjson"
import json, sys
with open(sys.argv[1],'r') as f:
    d=json.load(f)
def as_int(x):
    try: return int(x)
    except: return -1
best_prog=None; best=-1
for p in d.get("programs", []):
    t=p.get("tags",{}) or {}
    v=max(as_int(t.get("variant_bitrate")), as_int(t.get("BANDWIDTH")))
    if v>best:
        best=v; best_prog=p.get("program_id")
print(f"p:{best_prog}" if best_prog is not None and best>=0 else "")
PY
    )"
  else
    MAP_SEL=""
  fi
else
  MAP_SEL=""
fi
if [[ -n "$MAP_SEL" ]]; then
  echo "Using ffprobe map: -map $MAP_SEL"
  exec ffmpeg -loglevel info \
    "${HDRS_FF[@]}" \
    -rw_timeout 15000000 -reconnect 1 -reconnect_streamed 1 -reconnect_on_network_error 1 -reconnect_delay_max 5 \
    -i "$URL" \
    -map "$MAP_SEL" -map -0:d \
    -c:a copy -f adts "$OUT"
fi
echo "ffprobe produced no data; falling back to parsing master playlist…"
master="$(curl "${HDRS_CURL[@]}" "$URL")"
base="${URL%/*}"
readarray -t lines <<<"$master"
best_bw=0
best_child=""
prev_bw=0
for ((i=0; i<${#lines[@]}; i++)); do
  line="${lines[$i]}"
  if [[ "$line" =~ ^#EXT-X-STREAM-INF ]]; then
    if [[ "$line" =~ BANDWIDTH=([0-9]+) ]]; then
      prev_bw="${BASH_REMATCH[1]}"
    else
      prev_bw=0
    fi
    j=$((i+1))
    while (( j<${#lines[@]} )); do
      n="${lines[$j]}"
      if [[ -n "$n" && ! "$n" =~ ^# ]]; then
        if [[ "$n" =~ ^https?:// ]]; then child="$n"; else child="$base/$n"; fi
        if (( prev_bw > best_bw )); then
          best_bw="$prev_bw"; best_child="$child"
        fi
        break
      fi
      ((j++))
    done
  fi
done
if [[ -z "$best_child" ]]; then
  echo "Could not find any child playlists in master; aborting." >&2
  exit 3
fi
echo "Chose child playlist: $best_child (BANDWIDTH=$best_bw)"
exec ffmpeg -loglevel info \
  "${HDRS_FF[@]}" \
  -rw_timeout 15000000 -reconnect 1 -reconnect_streamed 1 -reconnect_on_network_error 1 -reconnect_delay_max 5 \
  -i "$best_child" \
  -map 0:a -map -0:d \
  -c:a copy -f adts "$OUT"
