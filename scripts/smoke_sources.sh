#!/usr/bin/env bash
set -euo pipefail

duration="${SMOKE_DURATION:-12}"
timeout_seconds="${SMOKE_TIMEOUT:-25}"
if [[ -n "${SMOKE_HEADERS:-}" ]]; then
  headers="$SMOKE_HEADERS"
else
  headers=$'Accept: */*\r\nCache-Control: no-cache\r\nPragma: no-cache\r\n'
fi
urls="${SMOKE_URLS:-}"

if [[ -z "$urls" ]]; then
  echo "SMOKE_URLS is required. Put one or more source URLs separated by newlines." >&2
  exit 2
fi

tmp_root="$(mktemp -d)"
cleanup() {
  rm -rf "$tmp_root"
}
trap cleanup EXIT

idx=0
ok=0
failed=0
mapfile -t source_urls <<< "$urls"

for url in "${source_urls[@]}"; do
  url="${url#"${url%%[![:space:]]*}"}"
  url="${url%"${url##*[![:space:]]}"}"
  [[ -z "$url" ]] && continue

  idx=$((idx + 1))
  out_dir="$tmp_root/source_$idx"
  mkdir -p "$out_dir"

  echo "== source $idx =="
  echo "$url"

  if ! ffprobe \
    -hide_banner -v error \
    -rw_timeout "$((timeout_seconds * 1000000))" \
    -user_agent "${FFMPEG_USER_AGENT:-Emby}" \
    -headers "$headers" \
    -show_entries stream=index,codec_type,codec_name,width,height,channels,sample_rate \
    -of compact=p=0:nk=1 \
    "$url"; then
    echo "ffprobe failed for source $idx" >&2
    failed=$((failed + 1))
    continue
  fi

  input_args=(
    -rw_timeout "$((timeout_seconds * 1000000))"
    -user_agent "${FFMPEG_USER_AGENT:-Emby}"
    -protocol_whitelist file,http,https,tcp,tls,crypto,data,pipe
    -headers "$headers"
    -fflags +discardcorrupt
    -thread_queue_size 4096
  )
  if [[ "${url,,}" == *.m3u8* ]]; then
    input_args+=(-http_persistent 0 -live_start_index -1)
  fi

  set +e
  timeout "$((duration + timeout_seconds + 5))s" ffmpeg \
    -nostdin -hide_banner -loglevel error -nostats -y \
    "${input_args[@]}" \
    -i "$url" \
    -map 0:v:0 -map 0:a:0? \
    -c:v copy \
    -c:a aac -b:a 160k -ar 48000 -ac 2 \
    -t "$duration" \
    -f hls \
    -hls_time 4 \
    -hls_list_size 4 \
    -hls_delete_threshold 10 \
    -hls_flags delete_segments+omit_endlist \
    -hls_segment_type mpegts \
    -hls_segment_filename "$out_dir/live_%06d.ts" \
    "$out_dir/index.m3u8"
  code=$?
  set -e

  segment_count="$(find "$out_dir" -maxdepth 1 -name 'live_*.ts' | wc -l | tr -d ' ')"
  if [[ "$code" -ne 0 || ! -s "$out_dir/index.m3u8" || "$segment_count" -lt 1 ]]; then
    echo "ffmpeg smoke failed for source $idx: exit=$code segments=$segment_count" >&2
    failed=$((failed + 1))
    continue
  fi

  echo "smoke ok for source $idx: segments=$segment_count"
  ok=$((ok + 1))
done

echo "== summary =="
echo "ok=$ok failed=$failed"

if [[ "$failed" -ne 0 || "$ok" -eq 0 ]]; then
  exit 1
fi
