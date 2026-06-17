from __future__ import annotations

import math
import os
import selectors
import shutil
import signal
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .m3u import M3UResolver
from .storage import JsonStore, hls_dir


@dataclass
class StreamRuntime:
    profile_id: str = ""
    status: str = "stopped"
    message: str = ""
    started_at_unix: int | None = None
    stopped_at_unix: int | None = None
    video_channel: str | None = None
    video_url: str | None = None
    audio_channel: str | None = None
    audio_url: str | None = None
    timeout_count: int = 0
    restart_count: int = 0
    last_exit_code: int | None = None
    hls_url_path: str = "/index.m3u8"
    snapshot_path: str | None = None
    logs: deque[str] = field(default_factory=lambda: deque(maxlen=1000))

    def as_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "status": self.status,
            "message": self.message,
            "started_at_unix": self.started_at_unix,
            "stopped_at_unix": self.stopped_at_unix,
            "video_channel": self.video_channel,
            "video_url": self.video_url,
            "audio_channel": self.audio_channel,
            "audio_url": self.audio_url,
            "timeout_count": self.timeout_count,
            "restart_count": self.restart_count,
            "last_exit_code": self.last_exit_code,
            "hls_url_path": self.hls_url_path,
            "snapshot_path": self.snapshot_path,
        }


class StreamManager:
    def __init__(self, store: JsonStore, resolver: M3UResolver | None = None, output_dir: Path | None = None):
        self.store = store
        self.resolver = resolver or M3UResolver()
        self.output_dir = output_dir or hls_dir()
        self.runtime = StreamRuntime()
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._proc: subprocess.Popen[str] | None = None

    def start(self, profile: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self.stop(wait=True)
            self._stop_event.clear()
            profile_id = str(profile.get("id") or "default")
            self.runtime = StreamRuntime(
                profile_id=profile_id,
                status="starting",
                message="resolving streams",
                started_at_unix=int(time.time()),
                hls_url_path="/index.m3u8",
            )
            self._thread = threading.Thread(target=self._run, args=(profile,), name="live-sync-stream", daemon=True)
            self._thread.start()
            return self.status()

    def stop(self, wait: bool = False) -> dict[str, Any]:
        self._stop_event.set()
        self._terminate_process()
        thread = self._thread
        if wait and thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=5)
        with self._lock:
            if self.runtime.status not in {"stopped", "error"}:
                self.runtime.status = "stopped"
                self.runtime.message = "stopped"
                self.runtime.stopped_at_unix = int(time.time())
        return self.status()

    def status(self) -> dict[str, Any]:
        with self._lock:
            return self.runtime.as_dict()

    def logs(self, limit: int = 200) -> list[str]:
        with self._lock:
            return list(self.runtime.logs)[-limit:]

    def capture_snapshot(self) -> dict[str, Any]:
        with self._lock:
            video_url = self.runtime.video_url
            profile_id = self.runtime.profile_id or "default"
        if not video_url:
            raise RuntimeError("stream is not running")

        snapshot_dir = self.store.root / "snapshots"
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        snapshot_path = snapshot_dir / f"{profile_id}-{int(time.time())}.jpg"
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            video_url,
            "-frames:v",
            "1",
            "-update",
            "1",
            str(snapshot_path),
        ]
        proc = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or "snapshot capture failed")
        with self._lock:
            self.runtime.snapshot_path = str(snapshot_path)
        return {"snapshot_path": str(snapshot_path)}

    def _run(self, profile: dict[str, Any]) -> None:
        try:
            self._run_until_stopped(profile)
        except Exception as exc:
            self._log(f"fatal: {exc}")
            with self._lock:
                self.runtime.status = "error"
                self.runtime.message = str(exc)
                self.runtime.stopped_at_unix = int(time.time())
            self._terminate_process()

    def _run_until_stopped(self, profile: dict[str, Any]) -> None:
        video_channels = selected_video_channels(profile)
        if not video_channels:
            raise ValueError("profile requires video.primary_channel or video fallback channels")
        audio_channel = required(profile, "audio", "channel")
        video_playlist = required(profile, "video", "playlist_url")
        audio_playlist = required(profile, "audio", "playlist_url")
        settings = profile.get("settings") if isinstance(profile.get("settings"), dict) else {}
        timeout_seconds = float(settings.get("timeout_seconds", 20))
        max_same_url_timeouts = int(settings.get("max_same_url_timeouts", 3))

        channel_index = 0
        consecutive_timeouts = 0
        last_video_url: str | None = None

        while not self._stop_event.is_set() and channel_index < len(video_channels):
            video_channel = video_channels[channel_index]
            video_entry = self.resolver.resolve(video_playlist, video_channel)
            audio_entry = self.resolver.resolve(audio_playlist, audio_channel)
            last_video_url = video_entry.url
            consecutive_timeouts = 0

            while not self._stop_event.is_set():
                exit_code, timed_out = self._launch_once(profile, video_channel, video_entry.url, audio_entry.url, timeout_seconds)
                if self._stop_event.is_set():
                    break
                if not timed_out and exit_code == 0:
                    with self._lock:
                        self.runtime.last_exit_code = exit_code
                    self._log("ffmpeg exited cleanly; retrying current channel")
                    time.sleep(2)
                    continue

                if not timed_out:
                    timed_out = True
                    self._log(f"ffmpeg exited with code {exit_code}; treating as failed stream")

                consecutive_timeouts += 1
                with self._lock:
                    self.runtime.timeout_count = consecutive_timeouts
                self._log(f"stream timeout {consecutive_timeouts}/{max_same_url_timeouts} on {video_channel}")
                if consecutive_timeouts < max_same_url_timeouts:
                    time.sleep(1)
                    continue

                self._log("timeout threshold reached; re-fetching playlists")
                try:
                    refreshed_video = self.resolver.resolve(video_playlist, video_channel)
                    refreshed_audio = self.resolver.resolve(audio_playlist, audio_channel)
                    audio_entry = refreshed_audio
                except Exception as exc:
                    self._log(f"playlist refresh failed: {exc}")
                    channel_index += 1
                    break

                if refreshed_video.url != last_video_url:
                    self._log(f"same channel URL changed; switching to refreshed URL for {video_channel}")
                    video_entry = refreshed_video
                    last_video_url = refreshed_video.url
                    consecutive_timeouts = 0
                    continue

                channel_index += 1
                if channel_index < len(video_channels):
                    self._log(f"same channel URL unchanged; falling back to {video_channels[channel_index]}")
                break

        if not self._stop_event.is_set():
            with self._lock:
                self.runtime.status = "stopped"
                self.runtime.message = "no selected video channel works"
                self.runtime.stopped_at_unix = int(time.time())
            self._log("stopped: no selected video channel works")

    def _launch_once(
        self,
        profile: dict[str, Any],
        video_channel: str,
        video_url: str,
        audio_url: str,
        timeout_seconds: float,
    ) -> tuple[int | None, bool]:
        self._prepare_output_dir()
        cmd = build_ffmpeg_hls_cmd(profile, video_url, audio_url, self.output_dir)
        with self._lock:
            self.runtime.status = "running"
            self.runtime.message = "ffmpeg running"
            self.runtime.video_channel = video_channel
            self.runtime.video_url = video_url
            self.runtime.audio_channel = required(profile, "audio", "channel")
            self.runtime.audio_url = audio_url
            self.runtime.restart_count += 1
        self._log("starting ffmpeg: " + redact_command(cmd))

        self._proc = subprocess.Popen(
            cmd,
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            bufsize=1,
            preexec_fn=os.setsid,
        )
        assert self._proc.stderr is not None

        timed_out = False
        last_activity = time.monotonic()
        selector = selectors.DefaultSelector()
        selector.register(self._proc.stderr, selectors.EVENT_READ)
        while not self._stop_event.is_set():
            if self._proc.poll() is not None:
                break

            events = selector.select(timeout=0.2)
            if events:
                line = self._proc.stderr.readline()
                if line:
                    last_activity = time.monotonic()
                    self._log(line.rstrip())
                    continue

            if self._hls_recently_updated(last_activity):
                last_activity = time.monotonic()

            if time.monotonic() - last_activity > timeout_seconds:
                timed_out = True
                self._log(f"ffmpeg produced no HLS or stderr activity for {timeout_seconds:.1f}s; treating as timeout")
                self._terminate_process()
                break
        selector.close()

        if self._stop_event.is_set():
            self._terminate_process()
        exit_code = self._proc.poll() if self._proc else None
        self._proc = None
        return exit_code, timed_out

    def _prepare_output_dir(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        for child in self.output_dir.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink(missing_ok=True)

    def _hls_recently_updated(self, since_monotonic: float) -> bool:
        wall_cutoff = time.time() - max(time.monotonic() - since_monotonic, 0)
        candidates = [self.output_dir / "index.m3u8"]
        candidates.extend(self.output_dir.glob("live_*.ts"))
        for path in candidates:
            try:
                if path.stat().st_mtime >= wall_cutoff:
                    return True
            except FileNotFoundError:
                continue
        return False

    def _terminate_process(self) -> None:
        proc = self._proc
        if not proc or proc.poll() is not None:
            return
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait(timeout=5)

    def _log(self, message: str) -> None:
        line = f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {message}"
        with self._lock:
            self.runtime.logs.append(line)


def selected_video_channels(profile: dict[str, Any]) -> list[str]:
    video = profile.get("video") if isinstance(profile.get("video"), dict) else {}
    channels: list[str] = []
    primary = str(video.get("primary_channel") or "").strip()
    if primary:
        channels.append(primary)
    for channel in video.get("fallback_channels") or []:
        text = str(channel).strip()
        if text and text not in channels:
            channels.append(text)
    return channels


def required(profile: dict[str, Any], section: str, field_name: str) -> str:
    value = profile.get(section, {}) if isinstance(profile.get(section), dict) else {}
    result = str(value.get(field_name) or "").strip()
    if not result:
        raise ValueError(f"profile requires {section}.{field_name}")
    return result


def build_ffmpeg_hls_cmd(profile: dict[str, Any], video_url: str, audio_url: str, output_dir: Path) -> list[str]:
    settings = profile.get("settings") if isinstance(profile.get("settings"), dict) else {}
    segment_time = str(settings.get("segment_time", 2))
    playlist_size = str(settings.get("playlist_size", 30))
    offset = float(profile.get("offset_seconds") or settings.get("offset_seconds") or 0)
    _list_size = max(20, math.ceil(max(offset, 0) / float(segment_time)) + 20)

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        str(settings.get("ffmpeg_loglevel", "warning")),
        "-y",
    ]
    if offset > 0.5:
        # A single ffmpeg process cannot sleep before attaching the delayed HLS input,
        # so the API path uses itsoffset for a simple runnable control-plane mux.
        # Existing scripts can still be used for exact delayed-video behavior.
        cmd += ["-itsoffset", f"{offset:.3f}"]
    cmd += [
        "-thread_queue_size",
        "4096",
        "-i",
        video_url,
        "-thread_queue_size",
        "4096",
        "-i",
        audio_url,
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c",
        "copy",
        "-tag:v",
        "hvc1",
        "-f",
        "hls",
        "-hls_time",
        segment_time,
        "-hls_list_size",
        playlist_size,
        "-hls_flags",
        "delete_segments+append_list+omit_endlist",
        "-hls_segment_filename",
        str(output_dir / "live_%06d.ts"),
        str(output_dir / "index.m3u8"),
    ]
    _ = _list_size
    return cmd


def redact_command(cmd: list[str]) -> str:
    return " ".join(cmd)
