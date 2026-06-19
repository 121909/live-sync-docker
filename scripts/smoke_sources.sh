#!/usr/bin/env bash
set -euo pipefail

duration="${SMOKE_DURATION:-12}"
timeout_seconds="${SMOKE_TIMEOUT:-120}"
hls_list_size="${SMOKE_HLS_LIST_SIZE:-60}"
if [[ -n "${SMOKE_HEADERS:-}" ]]; then
  headers="$SMOKE_HEADERS"
else
  headers=$'Accept: */*\r\nCache-Control: no-cache\r\nPragma: no-cache\r\n'
fi
urls="${SMOKE_URLS:-}"
names="${SMOKE_NAMES:-}"

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
mapfile -t source_names <<< "$names"

probe_json() {
  local url="$1"
  timeout "$((timeout_seconds + 5))s" ffprobe \
    -hide_banner -v error \
    -rw_timeout "$((timeout_seconds * 1000000))" \
    -user_agent "${FFMPEG_USER_AGENT:-Emby}" \
    -protocol_whitelist file,http,https,tcp,tls,crypto,data,pipe \
    -headers "$headers" \
    -show_entries stream=index,codec_type,codec_name,width,height,channels,sample_rate \
    -of json \
    "$url"
}

audio_index_from_probe() {
  python3 -c '
import json
import sys

try:
    data = json.load(sys.stdin)
except json.JSONDecodeError:
    print(0)
    raise SystemExit

audio = [stream for stream in data.get("streams", []) if stream.get("codec_type") == "audio"]
for idx, stream in enumerate(audio):
    if str(stream.get("codec_name", "")).lower() == "aac":
        print(idx)
        raise SystemExit
print(0)
'
}

segment_type_for_source() {
  local name="$1"
  local url="$2"
  local lowered="${name,,} ${url,,}"
  if [[ "$lowered" == *4k* ]]; then
    echo "fmp4"
  else
    echo "mpegts"
  fi
}

for url in "${source_urls[@]}"; do
  url="${url#"${url%%[![:space:]]*}"}"
  url="${url%"${url##*[![:space:]]}"}"
  [[ -z "$url" ]] && continue

  idx=$((idx + 1))
  out_dir="$tmp_root/source_$idx"
  mkdir -p "$out_dir"
  source_name="${source_names[$((idx - 1))]:-}"
  segment_type="$(segment_type_for_source "$source_name" "$url")"
  segment_ext=".ts"
  if [[ "$segment_type" == "fmp4" ]]; then
    segment_ext=".m4s"
  fi

  echo "== source $idx =="
  echo "$url"
  [[ -n "$source_name" ]] && echo "name=$source_name"
  echo "segment_type=$segment_type"

  probe_out="$out_dir/probe.json"
  if ! probe_json "$url" > "$probe_out"; then
    echo "ffprobe failed for source $idx" >&2
    failed=$((failed + 1))
    continue
  fi
  python3 - "$probe_out" <<'PY'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as f:
    data = json.load(f)
for stream in data.get("streams", []):
    fields = [
        str(stream.get("index", "")),
        str(stream.get("codec_type", "")),
        str(stream.get("codec_name", "")),
    ]
    if stream.get("codec_type") == "video":
        fields.extend([str(stream.get("width", "")), str(stream.get("height", ""))])
    if stream.get("codec_type") == "audio":
        fields.extend([str(stream.get("channels", "")), str(stream.get("sample_rate", ""))])
    print("|".join(fields))
PY
  audio_index="$(audio_index_from_probe < "$probe_out")"
  echo "audio_map=0:a:${audio_index}"

  input_args=(
    -rw_timeout "$((timeout_seconds * 1000000))"
    -user_agent "${FFMPEG_USER_AGENT:-Emby}"
    -protocol_whitelist file,http,https,tcp,tls,crypto,data,pipe
    -reconnect 1
    -reconnect_on_network_error 1
    -reconnect_streamed 1
    -reconnect_delay_max 10
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
    -map 0:v:0 -map "0:a:${audio_index}?" \
    -c copy \
    -t "$duration" \
    -f hls \
    -hls_time 4 \
    -hls_list_size "$hls_list_size" \
    -hls_delete_threshold "$((hls_list_size > 10 ? hls_list_size : 10))" \
    -hls_flags delete_segments+omit_endlist \
    -hls_segment_type "$segment_type" \
    -hls_segment_filename "$out_dir/live_%06d${segment_ext}" \
    "$out_dir/index.m3u8"
  code=$?
  set -e

  segment_count="$(find "$out_dir" -maxdepth 1 -name "live_*${segment_ext}" | wc -l | tr -d ' ')"
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
