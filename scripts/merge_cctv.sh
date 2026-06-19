#!/usr/bin/env bash
set -euo pipefail

VIDEO_URL="${VIDEO_URL:-}"
AUDIO_URL="${AUDIO_URL:-}"
OUTPUT="${OUTPUT:-/state/merged.mkv}"
SYNC_OFFSET="${SYNC_OFFSET:-0}"
DURATION="${DURATION:-}"
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-120}"

if [[ -z "$VIDEO_URL" || -z "$AUDIO_URL" ]]; then
  echo "VIDEO_URL and AUDIO_URL must be provided in the environment." >&2
  exit 2
fi

audio_index_for_url() {
  local url="$1"
  local probe
  probe="$(timeout "$((TIMEOUT_SECONDS + 5))s" ffprobe \
    -hide_banner -loglevel error \
    -rw_timeout "$((TIMEOUT_SECONDS * 1000000))" \
    -protocol_whitelist file,http,https,tcp,tls,crypto,data,pipe \
    -select_streams a \
    -show_entries stream=index,codec_name \
    -of json \
    "$url" 2>/dev/null)" || {
    echo 0
    return
  }
  python3 -c '
import json
import sys

try:
    data = json.load(sys.stdin)
except json.JSONDecodeError:
    print(0)
    raise SystemExit
streams = data.get("streams") or []
for idx, stream in enumerate(streams):
    if str(stream.get("codec_name") or "").lower() == "aac":
        print(idx)
        raise SystemExit
print(0)
' <<< "$probe"
}

VIDEO_AUDIO_INDEX="$(audio_index_for_url "$VIDEO_URL" || echo 0)"
AUDIO_INDEX="$(audio_index_for_url "$AUDIO_URL" || echo 0)"

ffmpeg_args=(
  -hide_banner
  -y
  -rw_timeout "$((TIMEOUT_SECONDS * 1000000))"
  -protocol_whitelist file,http,https,tcp,tls,crypto,data,pipe
  -reconnect 1
  -reconnect_on_network_error 1
  -reconnect_streamed 1
  -reconnect_delay_max 10
  -thread_queue_size 512
  -i "$VIDEO_URL"
)

if [[ "$SYNC_OFFSET" != "0" && -n "$SYNC_OFFSET" ]]; then
  ffmpeg_args+=(
    -itsoffset "$SYNC_OFFSET"
  )
fi

ffmpeg_args+=(
  -rw_timeout "$((TIMEOUT_SECONDS * 1000000))"
  -protocol_whitelist file,http,https,tcp,tls,crypto,data,pipe
  -reconnect 1
  -reconnect_on_network_error 1
  -reconnect_streamed 1
  -reconnect_delay_max 10
  -thread_queue_size 512
  -i "$AUDIO_URL"
)

if [[ -n "$DURATION" ]]; then
  ffmpeg_args+=(-t "$DURATION")
fi

ffmpeg_args+=(
  -map 0:v:0
  -map "1:a:${AUDIO_INDEX}"
  -map "0:a:${VIDEO_AUDIO_INDEX}?"
  -c copy
  -metadata:s:a:0 language=zho
  -metadata:s:a:0 title="Chinese commentary"
  -metadata:s:a:1 language=und
  -metadata:s:a:1 title="Original audio"
  -disposition:a:0 default
  "$OUTPUT"
)

exec ffmpeg "${ffmpeg_args[@]}"
