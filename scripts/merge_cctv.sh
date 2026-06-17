#!/usr/bin/env bash
set -euo pipefail

VIDEO_URL="${VIDEO_URL:-}"
AUDIO_URL="${AUDIO_URL:-}"
OUTPUT="${OUTPUT:-/state/merged.mkv}"
SYNC_OFFSET="${SYNC_OFFSET:-0}"
DURATION="${DURATION:-}"

if [[ -z "$VIDEO_URL" || -z "$AUDIO_URL" ]]; then
  echo "VIDEO_URL and AUDIO_URL must be provided in the environment." >&2
  exit 2
fi

ffmpeg_args=(
  -hide_banner
  -y
  -thread_queue_size 512
  -i "$VIDEO_URL"
)

if [[ "$SYNC_OFFSET" != "0" && -n "$SYNC_OFFSET" ]]; then
  ffmpeg_args+=(
    -itsoffset "$SYNC_OFFSET"
  )
fi

ffmpeg_args+=(
  -thread_queue_size 512
  -i "$AUDIO_URL"
)

if [[ -n "$DURATION" ]]; then
  ffmpeg_args+=(-t "$DURATION")
fi

ffmpeg_args+=(
  -map 0:v:0
  -map 1:a:0
  -map 0:a:0
  -c copy
  -metadata:s:a:0 language=zho
  -metadata:s:a:0 title="Chinese commentary"
  -metadata:s:a:1 language=und
  -metadata:s:a:1 title="Original audio"
  -disposition:a:0 default
  "$OUTPUT"
)

exec ffmpeg "${ffmpeg_args[@]}"
