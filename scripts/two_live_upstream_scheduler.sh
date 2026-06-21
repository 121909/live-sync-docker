#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_DIR="$ROOT_DIR/state"
WORK_DIR="$ROOT_DIR/work"
PID_FILE="$STATE_DIR/two_live_upstream_scheduler.pid"
LOG_FILE="$WORK_DIR/two_live_scheduler.log"
RESTART_SCRIPT="$ROOT_DIR/scripts/restart_two_live_upstream.py"

mkdir -p "$STATE_DIR" "$WORK_DIR"

find_scheduler_pid() {
  pgrep -f 'two_live_upstream_scheduler.sh run-loop' | head -n 1 || true
}

pid_is_scheduler() {
  local pid="$1"
  [[ -n "$pid" ]] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  local cmd
  cmd="$(ps -p "$pid" -o args= 2>/dev/null || true)"
  [[ "$cmd" == *"two_live_upstream_scheduler.sh run-loop"* ]]
}

is_running() {
  if [[ ! -f "$PID_FILE" ]]; then
    local discovered_pid
    discovered_pid="$(find_scheduler_pid)"
    if [[ -n "$discovered_pid" ]]; then
      printf '%s\n' "$discovered_pid" > "$PID_FILE"
      return 0
    fi
    return 1
  fi
  local pid
  pid="$(tr -d '[:space:]' < "$PID_FILE")"
  if pid_is_scheduler "$pid"; then
    return 0
  fi
  local discovered_pid
  discovered_pid="$(find_scheduler_pid)"
  if [[ -n "$discovered_pid" ]]; then
    printf '%s\n' "$discovered_pid" > "$PID_FILE"
    return 0
  fi
  return 1
}

next_sleep_seconds() {
  python3 - <<'PY'
import time
now = int(time.time())
next_slot = (now // 1800 + 1) * 1800
print(max(1, next_slot - now))
PY
}

run_loop() {
  trap 'rm -f "$PID_FILE"; exit 0' INT TERM EXIT
  echo "$$" > "$PID_FILE"
  while true; do
    sleep_seconds="$(next_sleep_seconds)"
    printf '[%s UTC] next upstream refresh in %ss\n' "$(date -u '+%F %T')" "$sleep_seconds"
    sleep "$sleep_seconds"
    printf '[%s UTC] refreshing 8800 upstream\n' "$(date -u '+%F %T')"
    python3 "$RESTART_SCRIPT"
  done
}

start_scheduler() {
  if is_running; then
    echo "scheduler already running: PID $(tr -d '[:space:]' < "$PID_FILE")"
    return 0
  fi
  rm -f "$PID_FILE"
  nohup setsid bash "$0" run-loop </dev/null >> "$LOG_FILE" 2>&1 &
  local launcher_pid="$!"
  printf '%s\n' "$launcher_pid" > "$PID_FILE"
  for _ in {1..5}; do
    if is_running; then
      echo "scheduler started: PID $(tr -d '[:space:]' < "$PID_FILE")"
      return 0
    fi
    sleep 1
  done
  echo "scheduler failed to start; see $LOG_FILE" >&2
  return 1
}

stop_scheduler() {
  if ! is_running; then
    rm -f "$PID_FILE"
    echo "scheduler not running"
    return 0
  fi
  local pid
  pid="$(tr -d '[:space:]' < "$PID_FILE")"
  if [[ -z "$pid" ]]; then
    pid="$(find_scheduler_pid)"
  fi
  kill -TERM -- "-$pid" 2>/dev/null || kill "$pid" 2>/dev/null || true
  for _ in {1..10}; do
    if ! kill -0 "$pid" 2>/dev/null; then
      rm -f "$PID_FILE"
      echo "scheduler stopped"
      return 0
    fi
    sleep 1
  done
  echo "scheduler stop timed out for PID $pid" >&2
  return 1
}

status_scheduler() {
  if is_running; then
    echo "scheduler running: PID $(tr -d '[:space:]' < "$PID_FILE")"
    return 0
  fi
  echo "scheduler not running"
  return 1
}

case "${1:-start}" in
  start)
    start_scheduler
    ;;
  stop)
    stop_scheduler
    ;;
  status)
    status_scheduler
    ;;
  run-loop)
    run_loop
    ;;
  *)
    echo "usage: $0 {start|stop|status|run-loop}" >&2
    exit 1
    ;;
esac
