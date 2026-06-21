#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


STATUS_SEGMENT_STALL_SECONDS = 50.0


@dataclass
class FetchResult:
    ok: bool
    latency_seconds: float
    http_code: int | None = None
    text: str = ""
    data: dict[str, Any] | None = None
    error: str = ""


class JsonlWriter:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fp = self.path.open("w", encoding="utf-8")
        self.events: list[dict[str, Any]] = []

    def write(self, event: dict[str, Any]) -> None:
        self.events.append(event)
        self.fp.write(json.dumps(event, ensure_ascii=False) + "\n")
        self.fp.flush()

    def close(self) -> None:
        self.fp.close()


class Monitor:
    def __init__(
        self,
        *,
        base_url: str,
        duration_seconds: int,
        sample_interval_seconds: int,
        heartbeat_interval_seconds: int,
        timeout_seconds: float,
        output_dir: Path,
    ):
        self.base_url = base_url.rstrip("/")
        self.status_url = f"{self.base_url}/api/status"
        self.playlist_url = f"{self.base_url}/index.m3u8"
        self.duration_seconds = duration_seconds
        self.sample_interval_seconds = sample_interval_seconds
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        self.timeout_seconds = timeout_seconds
        self.output_dir = output_dir

        parsed = urlparse(self.base_url)
        self.target_host = parsed.hostname or "127.0.0.1"
        self.target_port = parsed.port or (443 if parsed.scheme == "https" else 80)
        self.started_dt = datetime.now(timezone.utc)
        self.started_ts = iso_utc(self.started_dt)
        prefix = f"monitor_{self.target_port}_{self.started_ts.replace('-', '').replace(':', '')}"
        self.log_path = self.output_dir / f"{prefix}.jsonl"
        self.report_path = self.output_dir / f"{prefix}.report.md"
        self.writer = JsonlWriter(self.log_path)
        self.open_anomalies: dict[str, dict[str, Any]] = {}
        self.prev_values: dict[str, Any] = {}
        self.prev_status_last_segment_at = ""
        self.prev_status_last_segment_change_at = 0.0
        self.prev_playlist_last_segment = ""
        self.prev_playlist_last_segment_change_at = 0.0
        self.sample_count = 0
        self.monitor_start_monotonic = 0.0
        self.last_heartbeat_monotonic = 0.0

    def run(self) -> None:
        print(f"monitor target: {self.base_url}", flush=True)
        print(f"log path: {self.log_path}", flush=True)
        print(f"report path: {self.report_path}", flush=True)
        self.monitor_start_monotonic = time.monotonic()
        self.last_heartbeat_monotonic = self.monitor_start_monotonic
        self.writer.write(
            {
                "type": "monitor_start",
                "ts": self.started_ts,
                "duration_seconds": self.duration_seconds,
                "sample_interval_seconds": self.sample_interval_seconds,
                "heartbeat_interval_seconds": self.heartbeat_interval_seconds,
                "status_url": self.status_url,
                "playlist_url": self.playlist_url,
                "log_path": str(self.log_path),
            }
        )

        try:
            for sample_index in range((self.duration_seconds // self.sample_interval_seconds) + 1):
                deadline = self.monitor_start_monotonic + (sample_index * self.sample_interval_seconds)
                now_monotonic = time.monotonic()
                if deadline > now_monotonic:
                    time.sleep(deadline - now_monotonic)
                elapsed_seconds = time.monotonic() - self.monitor_start_monotonic
                if elapsed_seconds > self.duration_seconds + 0.25:
                    break
                self.collect_sample()
            self.complete()
        finally:
            self.writer.close()

    def collect_sample(self) -> None:
        sample_dt = datetime.now(timezone.utc)
        sample_ts = iso_utc(sample_dt)
        sample_monotonic = time.monotonic()
        elapsed_seconds = round(sample_monotonic - self.monitor_start_monotonic, 3)
        sample: dict[str, Any] = {
            "type": "sample",
            "ts": sample_ts,
            "elapsed_seconds": elapsed_seconds,
        }

        status_result = fetch_json(self.status_url, timeout_seconds=self.timeout_seconds)
        playlist_result = fetch_text(self.playlist_url, timeout_seconds=self.timeout_seconds)

        if status_result.ok and status_result.data is not None:
            status = status_result.data
            sample["status_http_code"] = status_result.http_code
            sample["status_latency_seconds"] = round(status_result.latency_seconds, 3)
            sample["running"] = bool(status.get("running", False))
            sample["stage"] = str(status.get("stage", "") or "")
            sample["failure_count"] = int(status.get("failure_count", 0) or 0)
            sample["last_error"] = str(status.get("last_error", "") or "")
            sample["started_at"] = status.get("started_at")
            sample["status_last_segment_at"] = status.get("last_segment_at")
            sample["active_channel"] = str(status.get("active_channel", "") or "")
            sample["active_audio_channel"] = str(status.get("active_audio_channel", "") or "")
            sample["auto_align_state"] = str(status.get("auto_align_state", "") or "")
            hls = status.get("hls") or {}
            sample["status_hls_latest_segment"] = str(hls.get("latest_segment", "") or "")
            sample["status_hls_segment_count"] = int(hls.get("segment_count", 0) or 0)
            self.log_state_changes(sample_ts, sample)
            self.set_anomaly(
                key="status_fetch_failed",
                is_open=False,
                severity="warn",
                message="",
                sample_ts=sample_ts,
                sample_monotonic=sample_monotonic,
            )
        else:
            sample["status_error"] = status_result.error
            self.set_anomaly(
                key="status_fetch_failed",
                is_open=True,
                severity="warn",
                message=f"status_fetch_failed: {status_result.error}",
                sample_ts=sample_ts,
                sample_monotonic=sample_monotonic,
            )

        if playlist_result.ok:
            playlist_media_sequence, playlist_last_segment, playlist_segment_count = parse_playlist(playlist_result.text)
            sample["playlist_http_code"] = playlist_result.http_code
            sample["playlist_latency_seconds"] = round(playlist_result.latency_seconds, 3)
            if playlist_media_sequence is not None:
                sample["playlist_media_sequence"] = playlist_media_sequence
            sample["playlist_last_segment"] = playlist_last_segment
            sample["playlist_segment_count"] = playlist_segment_count
            self.set_anomaly(
                key="playlist_fetch_failed",
                is_open=False,
                severity="warn",
                message="",
                sample_ts=sample_ts,
                sample_monotonic=sample_monotonic,
            )
        else:
            sample["playlist_error"] = playlist_result.error
            self.set_anomaly(
                key="playlist_fetch_failed",
                is_open=True,
                severity="warn",
                message=f"playlist_fetch_failed: {playlist_result.error}",
                sample_ts=sample_ts,
                sample_monotonic=sample_monotonic,
            )

        running = bool(sample.get("running", False))
        last_error = str(sample.get("last_error", "") or "")

        if "status_last_segment_at" in sample:
            status_last_segment_at = str(sample.get("status_last_segment_at") or "")
            if status_last_segment_at:
                if status_last_segment_at != self.prev_status_last_segment_at:
                    self.prev_status_last_segment_at = status_last_segment_at
                    self.prev_status_last_segment_change_at = sample_monotonic
                stall_seconds = round(sample_monotonic - self.prev_status_last_segment_change_at, 3)
                sample["status_last_segment_stall_seconds"] = stall_seconds
                if running and stall_seconds >= STATUS_SEGMENT_STALL_SECONDS:
                    self.set_anomaly(
                        key="status_segment_stalled",
                        is_open=True,
                        severity="warn",
                        message=f"status_segment_stalled: last_segment_at {status_last_segment_at} unchanged for {stall_seconds:.1f}s",
                        sample_ts=sample_ts,
                        sample_monotonic=sample_monotonic,
                    )
                else:
                    self.set_anomaly(
                        key="status_segment_stalled",
                        is_open=False,
                        severity="warn",
                        message="",
                        sample_ts=sample_ts,
                        sample_monotonic=sample_monotonic,
                    )
            else:
                if running:
                    self.set_anomaly(
                        key="status_segment_stalled",
                        is_open=True,
                        severity="warn",
                        message="status_segment_stalled: last_segment_at missing",
                        sample_ts=sample_ts,
                        sample_monotonic=sample_monotonic,
                    )
                else:
                    self.set_anomaly(
                        key="status_segment_stalled",
                        is_open=False,
                        severity="warn",
                        message="",
                        sample_ts=sample_ts,
                        sample_monotonic=sample_monotonic,
                    )

        playlist_last_segment = str(sample.get("playlist_last_segment", "") or "")
        if playlist_last_segment:
            if playlist_last_segment != self.prev_playlist_last_segment:
                self.prev_playlist_last_segment = playlist_last_segment
                self.prev_playlist_last_segment_change_at = sample_monotonic
            sample["playlist_stall_seconds"] = round(sample_monotonic - self.prev_playlist_last_segment_change_at, 3)

        if "running" in sample:
            self.set_anomaly(
                key="service_not_running",
                is_open=not running,
                severity="warn",
                message=f"service_not_running: running={running}, stage={sample.get('stage', '')}",
                sample_ts=sample_ts,
                sample_monotonic=sample_monotonic,
            )
        if "last_error" in sample:
            self.set_anomaly(
                key="last_error_present",
                is_open=bool(last_error),
                severity="warn",
                message=f"last_error_present: {last_error}" if last_error else "",
                sample_ts=sample_ts,
                sample_monotonic=sample_monotonic,
            )

        sample["open_anomalies"] = sorted(self.open_anomalies)
        self.writer.write(sample)
        self.sample_count += 1
        if sample_monotonic - self.last_heartbeat_monotonic >= self.heartbeat_interval_seconds:
            self.last_heartbeat_monotonic = sample_monotonic
            print(
                f"[{sample_ts}] elapsed={int(elapsed_seconds)}s running={sample.get('running')} "
                f"stage={sample.get('stage', '')} open={sample['open_anomalies']} "
                f"last_segment={sample.get('status_last_segment_at')} "
                f"playlist_last={sample.get('playlist_last_segment', '')}",
                flush=True,
            )

    def log_state_changes(self, sample_ts: str, sample: dict[str, Any]) -> None:
        watched_fields = (
            "running",
            "stage",
            "started_at",
            "failure_count",
            "last_error",
            "active_channel",
            "active_audio_channel",
            "auto_align_state",
        )
        for field in watched_fields:
            current = sample.get(field)
            previous = self.prev_values.get(field, _MISSING)
            if previous is _MISSING:
                self.prev_values[field] = current
                continue
            if previous == current:
                continue
            severity = state_change_severity(field, previous, current)
            message = state_change_message(field, previous, current)
            self.writer.write(
                {
                    "type": "state_change",
                    "severity": severity,
                    "ts": sample_ts,
                    "message": message,
                    "field": field,
                    "previous": None if previous is _MISSING else previous,
                    "current": current,
                }
            )
            self.prev_values[field] = current

    def set_anomaly(
        self,
        *,
        key: str,
        is_open: bool,
        severity: str,
        message: str,
        sample_ts: str,
        sample_monotonic: float,
    ) -> None:
        if is_open:
            current = self.open_anomalies.get(key)
            if current is None:
                self.open_anomalies[key] = {
                    "opened_ts": sample_ts,
                    "opened_monotonic": sample_monotonic,
                    "severity": severity,
                    "message": message,
                }
                self.writer.write(
                    {
                        "type": "anomaly_open",
                        "severity": severity,
                        "ts": sample_ts,
                        "message": message,
                        "key": key,
                    }
                )
            else:
                current["message"] = message or current["message"]
            return

        current = self.open_anomalies.pop(key, None)
        if current is None:
            return
        duration_seconds = round(sample_monotonic - current["opened_monotonic"], 3)
        self.writer.write(
            {
                "type": "anomaly_clear",
                "severity": "info",
                "ts": sample_ts,
                "message": f"{key}: recovered after {duration_seconds:.1f}s",
                "key": key,
                "duration_seconds": duration_seconds,
            }
        )

    def complete(self) -> None:
        complete_ts = iso_utc(datetime.now(timezone.utc))
        self.writer.write(
            {
                "type": "monitor_complete",
                "ts": complete_ts,
                "duration_seconds": round(time.monotonic() - self.monitor_start_monotonic, 3),
                "sample_count": self.sample_count,
                "open_anomalies": sorted(self.open_anomalies),
                "anomaly_event_count": sum(
                    1 for event in self.writer.events if event["type"] in {"anomaly_open", "anomaly_clear"}
                ),
                "log_path": str(self.log_path),
            }
        )
        post_status = fetch_json(self.status_url, timeout_seconds=self.timeout_seconds)
        self.write_report(post_status=post_status)
        print(f"monitor complete: {self.report_path}", flush=True)

    def write_report(self, *, post_status: FetchResult) -> None:
        open_counts = Counter(event["key"] for event in self.writer.events if event["type"] == "anomaly_open")
        state_changes = [event for event in self.writer.events if event["type"] == "state_change"]
        samples = [event for event in self.writer.events if event["type"] == "sample"]
        status_stall_periods = collect_anomaly_windows(self.writer.events, "status_segment_stalled")
        playlist_fail_periods = collect_anomaly_windows(self.writer.events, "playlist_fetch_failed")
        status_fail_periods = collect_anomaly_windows(self.writer.events, "status_fetch_failed")
        service_not_running_periods = collect_anomaly_windows(self.writer.events, "service_not_running")
        restart_changes = [
            event
            for event in state_changes
            if event["field"] == "started_at" and event.get("current") not in (None, "")
        ]
        failure_count_increases = [
            event
            for event in state_changes
            if event["field"] == "failure_count" and int(event.get("current") or 0) > int(event.get("previous") or 0)
        ]
        last_error_warns = [
            event
            for event in state_changes
            if event["field"] == "last_error" and str(event.get("current") or "")
        ]
        capture_failed_events = [
            event
            for event in state_changes
            if event["field"] == "auto_align_state" and event.get("current") == "capture_failed"
        ]
        unstable = bool(
            open_counts
            or restart_changes
            or failure_count_increases
            or last_error_warns
            or capture_failed_events
        )

        lines: list[str] = []
        lines.append(f"# {self.target_port} monitor report")
        lines.append("")
        lines.append("## Window")
        lines.append("")
        lines.append(f"- Target: `{self.target_host}:{self.target_port}`")
        lines.append(f"- Monitor start: `{self.started_ts}`")
        lines.append(f"- Monitor end: `{iso_utc(datetime.now(timezone.utc))}`")
        lines.append(f"- Sample interval: `{self.sample_interval_seconds}s`")
        lines.append(f"- Samples collected: `{self.sample_count}`")
        lines.append(f"- Raw evidence: `{self.log_path}`")
        lines.append("")
        lines.append("## Verified after monitor")
        lines.append("")
        if post_status.ok and post_status.data is not None:
            data = post_status.data
            hls = data.get("hls") or {}
            lines.append("- Immediate post-window check succeeded.")
            lines.append(f"- `running={bool(data.get('running', False))}`")
            lines.append(f"- `stage={data.get('stage', '')}`")
            lines.append(f"- `started_at={data.get('started_at')}`")
            lines.append(f"- `last_segment_at={data.get('last_segment_at')}`")
            lines.append(f"- `hls.latest_segment={hls.get('latest_segment', '')}`")
            lines.append(f"- `failure_count={data.get('failure_count', 0)}`")
            lines.append(f"- `last_error={json.dumps(str(data.get('last_error', '') or ''))}`")
        else:
            lines.append(f"- Post-window status check failed: `{post_status.error}`")
        lines.append("")
        lines.append("## Findings")
        lines.append("")

        finding_index = 1
        if status_stall_periods:
            longest = max(status_stall_periods, key=lambda item: item["duration_seconds"])
            lines.append(f"### {finding_index}. Repeated status timestamp stalls")
            lines.append("")
            lines.append(
                f"`last_segment_at` stalled `{len(status_stall_periods)}` times. Longest observed stall was "
                f"`{longest['duration_seconds']:.1f}s`."
            )
            lines.append("")
            lines.append("Notable windows:")
            for window in status_stall_periods[:5]:
                lines.append(
                    f"- `{window['start_ts']}` to `{window['end_ts']}` (`{window['duration_seconds']:.1f}s`)"
                )
            lines.append("")
            finding_index += 1

        if restart_changes or service_not_running_periods or status_fail_periods or playlist_fail_periods:
            lines.append(f"### {finding_index}. Service interruption or restart signals")
            lines.append("")
            if restart_changes:
                lines.append(
                    f"`started_at` changed `{len(restart_changes)}` times, which indicates at least that many restart cycles."
                )
                lines.append("")
                lines.append("Observed `started_at` changes:")
                for event in restart_changes[:8]:
                    lines.append(f"- `{event['ts']}`: `{event['previous']}` -> `{event['current']}`")
                lines.append("")
            interruption_windows = summarize_interruption_windows(
                service_not_running_periods=service_not_running_periods,
                status_fail_periods=status_fail_periods,
                playlist_fail_periods=playlist_fail_periods,
            )
            if interruption_windows:
                lines.append("Observed interruption windows:")
                for message in interruption_windows[:8]:
                    lines.append(f"- {message}")
                lines.append("")
            finding_index += 1

        if last_error_warns or failure_count_increases:
            lines.append(f"### {finding_index}. Upstream cache or pipeline errors")
            lines.append("")
            if last_error_warns:
                for event in last_error_warns[:6]:
                    lines.append(f"- `{event['ts']}`: `{event['current']}`")
            if failure_count_increases:
                for event in failure_count_increases[:6]:
                    lines.append(
                        f"- `{event['ts']}`: `failure_count` changed `{event['previous']}` -> `{event['current']}`"
                    )
            lines.append("")
            finding_index += 1

        if capture_failed_events:
            lines.append(f"### {finding_index}. Auto-align instability")
            lines.append("")
            lines.append(
                f"`auto_align_state` entered `capture_failed` `{len(capture_failed_events)}` times during the window."
            )
            lines.append("")
            for event in capture_failed_events[:8]:
                lines.append(f"- `{event['ts']}`")
            lines.append("")
            finding_index += 1

        if finding_index == 1:
            lines.append("No anomalies were detected in the monitor window.")
            lines.append("")

        lines.append("## Counts from raw log")
        lines.append("")
        for key, count in sorted(open_counts.items()):
            lines.append(f"- `anomaly_open={key}`: `{count}`")
        state_change_counts = Counter(event["field"] for event in state_changes)
        for field, count in sorted(state_change_counts.items()):
            if field in {"started_at", "failure_count", "last_error"}:
                lines.append(f"- `state_change={field}`: `{count}`")
        lines.append("")
        lines.append("## Conclusion")
        lines.append("")
        if unstable:
            lines.append(
                f"The program on port `{self.target_port}` was not stable across the full "
                f"`{self.duration_seconds // 60}`-minute window."
            )
        else:
            lines.append(
                f"The program on port `{self.target_port}` stayed stable across the full "
                f"`{self.duration_seconds // 60}`-minute window and no anomalies were detected."
            )
        lines.append("")
        self.report_path.write_text("\n".join(lines), encoding="utf-8")


_MISSING = object()


def state_change_severity(field: str, previous: Any, current: Any) -> str:
    if field == "started_at":
        return "warn"
    if field == "running":
        return "warn" if not bool(current) else "info"
    if field == "stage":
        return "warn" if current in {"stopped", "restarting"} else "info"
    if field == "failure_count":
        return "warn" if int(current or 0) > int(previous or 0) else "info"
    if field == "last_error":
        return "warn" if str(current or "") else "info"
    return "info"


def state_change_message(field: str, previous: Any, current: Any) -> str:
    return f"{field} changed {json.dumps(previous, ensure_ascii=False)} -> {json.dumps(current, ensure_ascii=False)}"


def fetch_json(url: str, *, timeout_seconds: float) -> FetchResult:
    result = fetch_text(url, timeout_seconds=timeout_seconds)
    if not result.ok:
        return result
    try:
        result.data = json.loads(result.text)
    except json.JSONDecodeError as exc:
        return FetchResult(ok=False, latency_seconds=result.latency_seconds, error=f"JSONDecodeError: {exc}")
    return result


def fetch_text(url: str, *, timeout_seconds: float) -> FetchResult:
    started = time.monotonic()
    request = urllib.request.Request(url, headers={"User-Agent": "live-sync-monitor/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            body = response.read().decode("utf-8", errors="replace")
            return FetchResult(
                ok=True,
                latency_seconds=time.monotonic() - started,
                http_code=response.getcode(),
                text=body,
            )
    except urllib.error.HTTPError as exc:
        return FetchResult(
            ok=False,
            latency_seconds=time.monotonic() - started,
            http_code=exc.code,
            error=f"HTTPError: {exc}",
        )
    except Exception as exc:  # noqa: BLE001
        return FetchResult(
            ok=False,
            latency_seconds=time.monotonic() - started,
            error=f"{type(exc).__name__}: {exc}",
        )


def parse_playlist(text: str) -> tuple[int | None, str, int]:
    media_sequence = None
    segments: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            _, _, value = line.partition(":")
            try:
                media_sequence = int(value.strip())
            except ValueError:
                media_sequence = None
            continue
        if not line.startswith("#"):
            segments.append(line)
    return media_sequence, segments[-1] if segments else "", len(segments)


def collect_anomaly_windows(events: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    windows: list[dict[str, Any]] = []
    opened: dict[str, Any] | None = None
    for event in events:
        if event["type"] == "anomaly_open" and event.get("key") == key:
            opened = event
        elif event["type"] == "anomaly_clear" and event.get("key") == key and opened is not None:
            windows.append(
                {
                    "start_ts": opened["ts"],
                    "end_ts": event["ts"],
                    "duration_seconds": float(event.get("duration_seconds", 0.0) or 0.0),
                    "message": opened.get("message", ""),
                }
            )
            opened = None
    if opened is not None:
        windows.append(
            {
                "start_ts": opened["ts"],
                "end_ts": "open",
                "duration_seconds": 0.0,
                "message": opened.get("message", ""),
            }
        )
    return windows


def summarize_interruption_windows(
    *,
    service_not_running_periods: list[dict[str, Any]],
    status_fail_periods: list[dict[str, Any]],
    playlist_fail_periods: list[dict[str, Any]],
) -> list[str]:
    lines: list[str] = []
    for window in service_not_running_periods[:4]:
        lines.append(
            f"`{window['start_ts']}` to `{window['end_ts']}`: service reported not running (`{window['duration_seconds']:.1f}s`)"
        )
    for window in status_fail_periods[:4]:
        lines.append(
            f"`{window['start_ts']}` to `{window['end_ts']}`: `/api/status` failed (`{window['duration_seconds']:.1f}s`)"
        )
    for window in playlist_fail_periods[:4]:
        lines.append(
            f"`{window['start_ts']}` to `{window['end_ts']}`: `/index.m3u8` failed (`{window['duration_seconds']:.1f}s`)"
        )
    return lines


def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Monitor live-sync status and HLS playlist for anomalies.")
    parser.add_argument("--base-url", default="http://127.0.0.1:18081", help="Base URL of the live-sync service.")
    parser.add_argument("--duration", type=int, default=1800, help="Monitor duration in seconds.")
    parser.add_argument("--sample-interval", type=int, default=10, help="Sampling interval in seconds.")
    parser.add_argument("--heartbeat-interval", type=int, default=30, help="Heartbeat print interval in seconds.")
    parser.add_argument("--timeout", type=float, default=5.0, help="HTTP timeout in seconds.")
    parser.add_argument("--output-dir", default="work", help="Directory for jsonl and report output.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    monitor = Monitor(
        base_url=args.base_url,
        duration_seconds=args.duration,
        sample_interval_seconds=args.sample_interval,
        heartbeat_interval_seconds=args.heartbeat_interval,
        timeout_seconds=args.timeout,
        output_dir=Path(args.output_dir),
    )
    monitor.run()


if __name__ == "__main__":
    main()
