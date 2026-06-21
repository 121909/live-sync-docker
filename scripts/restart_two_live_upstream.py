#!/usr/bin/env python3
"""Refresh only the 8800 two-live upstream and keep downstream untouched."""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import signal
import socket
import subprocess
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen


REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = REPO_ROOT / "configs" / "two_live_upstream.json"
LOCK_PATH = REPO_ROOT / "state" / "two_live_upstream_restart.lock"
PID_PATH = REPO_ROOT / "state" / "two_live_upstream.pid"
RUNNER_LOG = REPO_ROOT / "work" / "two_live_runner.log"
UPSTREAM_ROOT = REPO_ROOT / "work" / "two_live"
DEFAULT_PORT = 8800
READY_URLS = (
    "http://127.0.0.1:8800/A/master.m3u8",
    "http://127.0.0.1:8800/B/master.m3u8",
)
MARKER_PATHS = (
    UPSTREAM_ROOT / "A" / "current.txt",
    UPSTREAM_ROOT / "B" / "current.txt",
)


def log(message: str) -> None:
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    print(f"[{timestamp} UTC] {message}", flush=True)


def read_cmdline(pid: int) -> list[str] | None:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except FileNotFoundError:
        return None
    parts = [part.decode("utf-8", errors="replace") for part in raw.split(b"\0") if part]
    return parts or None


def find_flag_value(cmd: list[str], flag: str) -> str | None:
    try:
        idx = cmd.index(flag)
    except ValueError:
        return None
    return cmd[idx + 1] if idx + 1 < len(cmd) else None


def matches_upstream(cmd: list[str]) -> bool:
    if not cmd:
        return False
    if not any(part.endswith("serve_two_live.py") for part in cmd):
        return False
    port_value = find_flag_value(cmd, "--port")
    return port_value == str(DEFAULT_PORT)


def find_running_upstream() -> tuple[int, list[str]] | None:
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        cmd = read_cmdline(pid)
        if cmd and matches_upstream(cmd):
            return pid, cmd
    return None


def find_upstream_ffmpeg_pids() -> list[int]:
    pids: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        cmd = read_cmdline(pid)
        if not cmd:
            continue
        if Path(cmd[0]).name != "ffmpeg":
            continue
        if any("work/two_live/" in part for part in cmd):
            pids.append(pid)
    return pids


def save_fallback_command(cmd: list[str]) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps({"command": cmd}, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def load_fallback_command() -> list[str]:
    data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    command = data.get("command")
    if not isinstance(command, list) or not command:
        raise RuntimeError(f"invalid command in {CONFIG_PATH}")
    return [str(item) for item in command]


def is_pid_alive(pid: int) -> bool:
    return Path(f"/proc/{pid}").exists()


def wait_for_pid_exit(pid: int, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not is_pid_alive(pid):
            return True
        time.sleep(0.2)
    return not is_pid_alive(pid)


def stop_existing_upstream() -> None:
    running = find_running_upstream()
    if not running:
        return
    pid, cmd = running
    log(f"stopping upstream pid={pid}: {' '.join(cmd)}")
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    if wait_for_pid_exit(pid, timeout=15):
        return
    log(f"upstream pid={pid} did not exit after SIGTERM; sending SIGKILL")
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    wait_for_pid_exit(pid, timeout=5)


def stop_stale_upstream_ffmpeg() -> None:
    stale = find_upstream_ffmpeg_pids()
    if not stale:
        return
    log(f"stopping stale upstream ffmpeg pids={','.join(str(pid) for pid in stale)}")
    for pid in stale:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
    deadline = time.time() + 5
    while time.time() < deadline:
        alive = [pid for pid in stale if is_pid_alive(pid)]
        if not alive:
            return
        time.sleep(0.2)
    for pid in stale:
        if not is_pid_alive(pid):
            continue
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def port_accepting(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def wait_for_listener_drop(port: int, timeout: float) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not port_accepting(port):
            return
        time.sleep(0.2)
    raise RuntimeError(f"port {port} still accepting connections after shutdown")


def clear_upstream_root() -> None:
    shutil.rmtree(UPSTREAM_ROOT, ignore_errors=True)
    UPSTREAM_ROOT.mkdir(parents=True, exist_ok=True)


def start_upstream(cmd: list[str]) -> subprocess.Popen[str]:
    RUNNER_LOG.parent.mkdir(parents=True, exist_ok=True)
    log_handle = RUNNER_LOG.open("w", encoding="utf-8")
    proc = subprocess.Popen(
        cmd,
        cwd=REPO_ROOT,
        stdin=subprocess.DEVNULL,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    PID_PATH.write_text(f"{proc.pid}\n", encoding="utf-8")
    return proc


def url_ready(url: str) -> bool:
    try:
        with urlopen(url, timeout=2) as resp:
            body = resp.read(64).decode("utf-8", errors="replace")
    except (URLError, OSError):
        return False
    return body.startswith("#EXTM3U")


def markers_refreshed(after_ts: float) -> bool:
    for path in MARKER_PATHS:
        try:
            stat = path.stat()
        except FileNotFoundError:
            return False
        if stat.st_size <= 0 or stat.st_mtime < after_ts:
            return False
    return True


def wait_for_ready(proc: subprocess.Popen[str], timeout: float, *, after_ts: float) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"upstream exited with code {proc.returncode}; see {RUNNER_LOG}")
        if markers_refreshed(after_ts) and all(url_ready(url) for url in READY_URLS):
            return
        time.sleep(0.5)
    raise RuntimeError(f"upstream did not become ready within {timeout:.0f}s; see {RUNNER_LOG}")


def wait_for_reload(pid: int, timeout: float, *, after_ts: float) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not is_pid_alive(pid):
            raise RuntimeError(f"upstream pid {pid} exited during reload")
        if markers_refreshed(after_ts) and all(url_ready(url) for url in READY_URLS):
            return
        time.sleep(0.5)
    raise RuntimeError(f"upstream reload did not become ready within {timeout:.0f}s")


def resolve_command() -> list[str]:
    running = find_running_upstream()
    if running:
        _pid, cmd = running
        save_fallback_command(cmd)
        return cmd
    if CONFIG_PATH.exists():
        return load_fallback_command()
    raise RuntimeError("no running 8800 upstream found and no fallback config is available")


def reload_existing_upstream(pid: int) -> None:
    log(f"reloading upstream pid={pid} via SIGHUP")
    marker = time.time()
    os.kill(pid, signal.SIGHUP)
    wait_for_reload(pid, timeout=30, after_ts=marker)


def full_restart(command: list[str]) -> None:
    stop_existing_upstream()
    stop_stale_upstream_ffmpeg()
    wait_for_listener_drop(DEFAULT_PORT, timeout=15)
    clear_upstream_root()
    marker = time.time()
    proc = start_upstream(command)
    wait_for_ready(proc, timeout=30, after_ts=marker)


def main() -> int:
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("w", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("another upstream restart is already in progress")
            return 0

        running = find_running_upstream()
        command = resolve_command()
        log(f"using upstream command: {' '.join(command)}")
        if running:
            pid, _cmd = running
            try:
                reload_existing_upstream(pid)
                log(f"upstream reloaded successfully on port {DEFAULT_PORT}")
                return 0
            except Exception as exc:
                log(f"in-place reload failed: {exc}; falling back to full restart")
        full_restart(command)
        log(f"upstream restarted successfully on port {DEFAULT_PORT}")
        return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        log(f"restart failed: {exc}")
        raise SystemExit(1)
