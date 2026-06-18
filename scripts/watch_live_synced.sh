#!/usr/bin/env bash
set -euo pipefail

VIDEO_URL="${VIDEO_URL:-}"
AUDIO_URL="${AUDIO_URL:-}"
OFFSET_STATE="${OFFSET_STATE:-/state/last_sync_offset.json}"
SYNC_OFFSET="${SYNC_OFFSET:-}"
MODE="${MODE:-hls}"
OUT_DIR="${OUT_DIR:-/hls}"
WORK_DIR="${WORK_DIR:-/tmp/live_4k_delay}"
PORT="${PORT:-18080}"
SEGMENT_TIME="${SEGMENT_TIME:-2}"
PLAYLIST_SIZE="${PLAYLIST_SIZE:-30}"
DEFAULT_OFFSET="${DEFAULT_OFFSET:-}"
SERVE_HLS="${SERVE_HLS:-1}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ -z "$VIDEO_URL" || -z "$AUDIO_URL" ]]; then
  echo "VIDEO_URL and AUDIO_URL must be provided in the environment." >&2
  exit 2
fi

read_offset_state() {
  python3 - "$OFFSET_STATE" <<'PY'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as f:
    data = json.load(f)
print(float(data["offset_seconds"]))
PY
}

if [[ -z "$SYNC_OFFSET" ]]; then
  if [[ -f "$OFFSET_STATE" ]]; then
    SYNC_OFFSET="$(read_offset_state)"
    echo "using saved sync offset: ${SYNC_OFFSET}s from ${OFFSET_STATE}"
  elif [[ -n "$DEFAULT_OFFSET" ]]; then
    SYNC_OFFSET="$DEFAULT_OFFSET"
    echo "using default sync offset: ${SYNC_OFFSET}s"
  else
    echo "no sync offset found." >&2
    echo "configure a valid OCR provider in WebUI first, or start with SYNC_OFFSET=25." >&2
    exit 2
  fi
else
  echo "using manual sync offset: ${SYNC_OFFSET}s"
fi

OFFSET="$(python3 - "$SYNC_OFFSET" <<'PY'
import sys

offset = float(sys.argv[1])
if offset < -0.001:
    raise SystemExit("negative offsets are not supported by this live watcher")
print(f"{max(offset, 0):.3f}")
PY
)"

LIST_SIZE="$(OFFSET="$OFFSET" SEGMENT_TIME="$SEGMENT_TIME" python3 - <<'PY'
import math
import os

offset = float(os.environ["OFFSET"])
segment = float(os.environ["SEGMENT_TIME"])
print(max(20, math.ceil(offset / segment) + 20))
PY
)"

rm -rf "$WORK_DIR"
mkdir -p "$WORK_DIR"

cleanup_pids=()
cleanup() {
  for pid in "${cleanup_pids[@]:-}"; do
    kill "$pid" 2>/dev/null || true
  done
}
trap cleanup EXIT INT TERM

if python3 - "$OFFSET" <<'PY'
import sys
raise SystemExit(0 if float(sys.argv[1]) < 0.5 else 1)
PY
then
  DELAY_INPUT="$VIDEO_URL"
  DELAY_INPUT_ARGS=(-thread_queue_size 4096 -i "$DELAY_INPUT")
  echo "offset is near zero; using the 4K source directly"
else
  DELAY_PLAYLIST="$WORK_DIR/4k_delay.m3u8"
  echo "buffering 4K video for ${OFFSET}s before playback..."

  start_time="$(python3 - <<'PY'
import time
print(time.monotonic())
PY
)"

  ffmpeg \
    -hide_banner -loglevel warning -y \
    -thread_queue_size 4096 -i "$VIDEO_URL" \
    -map 0:v:0 -map 0:a:0? \
    -c copy \
    -tag:v hvc1 \
    -f hls \
    -hls_time "$SEGMENT_TIME" \
    -hls_list_size "$LIST_SIZE" \
    -hls_flags delete_segments+append_list+omit_endlist \
    -hls_segment_filename "$WORK_DIR/seg_%06d.ts" \
    "$DELAY_PLAYLIST" &
  recorder_pid="$!"
  cleanup_pids+=("$recorder_pid")

  until [[ -s "$DELAY_PLAYLIST" ]]; do
    if ! kill -0 "$recorder_pid" 2>/dev/null; then
      echo "4K delay recorder exited before creating a playlist" >&2
      exit 1
    fi
    sleep 0.2
  done

  remaining="$(python3 - "$start_time" "$OFFSET" <<'PY'
import sys
import time

start = float(sys.argv[1])
offset = float(sys.argv[2])
remaining = offset - (time.monotonic() - start)
print(f"{max(remaining, 0):.3f}")
PY
)"
  sleep "$remaining"
  DELAY_INPUT="$DELAY_PLAYLIST"
  DELAY_INPUT_ARGS=(-thread_queue_size 4096 -live_start_index 0 -i "$DELAY_INPUT")
fi

run_mux_to_hls() {
  mkdir -p "$OUT_DIR"
  find "$OUT_DIR" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +

  if [[ "$SERVE_HLS" != "0" ]]; then
    python3 "$SCRIPT_DIR/serve_hls.py" "$PORT" "$OUT_DIR" >/tmp/live_synced_http.log 2>&1 &
    server_pid="$!"
    cleanup_pids+=("$server_pid")
  fi

  echo "open this in VLC/mpv/IINA:"
  echo "  http://127.0.0.1:${PORT}/index.m3u8"
  echo "replace 127.0.0.1 with this server's IP if watching from another machine."

  ffmpeg \
    -hide_banner -loglevel warning -y \
    -re "${DELAY_INPUT_ARGS[@]}" \
    -thread_queue_size 4096 -i "$AUDIO_URL" \
    -map 0:v:0 -map 1:a:0 \
    -c copy \
    -tag:v hvc1 \
    -f hls \
    -hls_time "$SEGMENT_TIME" \
    -hls_list_size "$PLAYLIST_SIZE" \
    -hls_flags delete_segments+append_list+omit_endlist \
    -hls_segment_filename "$OUT_DIR/live_%06d.ts" \
    "$OUT_DIR/index.m3u8"
}

run_mux_to_ffplay() {
  ffmpeg \
    -hide_banner -loglevel warning \
    -re "${DELAY_INPUT_ARGS[@]}" \
    -thread_queue_size 4096 -i "$AUDIO_URL" \
    -map 0:v:0 -map 1:a:0 \
    -c copy \
    -f matroska - | ffplay -hide_banner -loglevel warning -i -
}

case "$MODE" in
  hls)
    run_mux_to_hls
    ;;
  ffplay)
    run_mux_to_ffplay
    ;;
  *)
    echo "unknown MODE=${MODE}; use MODE=hls or MODE=ffplay" >&2
    exit 2
    ;;
esac
