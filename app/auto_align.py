import tempfile
import time
from dataclasses import dataclass
from typing import Any


ALIGN_STATE_WAITING = "waiting"
ALIGN_STATE_PROBING = "probing"
ALIGN_STATE_ALIGNED = "aligned"
ALIGN_STATE_DISABLED = "disabled"
ALIGN_STATE_CAPTURE_FAILED = "capture_failed"
ALIGN_RETRY_BACKOFF_BASE_SECONDS = 45
ALIGN_RETRY_BACKOFF_MAX_SECONDS = 600
ALIGN_MAX_CAPTURE_SKEW_SECONDS = 1.0
ALIGN_MAX_FINISH_DELTA_SECONDS = 1.0
ALIGN_CANDIDATE_SAMPLE_COUNT = 1
ALIGN_SAMPLE_SPACING_SECONDS = 0.0


@dataclass
class AlignmentMonitor:
    video_clock: str = ""
    audio_clock: str = ""
    candidate_video_clock: str = ""
    candidate_audio_clock: str = ""
    candidate_message: str = ""
    candidate_video_seconds_back: float | None = None
    candidate_audio_seconds_back: float | None = None
    verify_video_clock: str = ""
    verify_audio_clock: str = ""
    verify_message: str = ""
    verify_delta: float | None = None
    verify_video_seconds_back: float | None = None
    verify_audio_seconds_back: float | None = None
    state: str = ALIGN_STATE_WAITING
    message: str = "waiting for next auto-align probe"
    mismatch_count: int = 0
    checks: int = 0
    video_missing_count: int = 0
    audio_missing_count: int = 0
    next_probe_at: float = 0.0
    retry_backoff_seconds: int = 0
    consecutive_failures: int = 0
    last_successful_offset: float | None = None
    last_candidate_offset: float | None = None
    last_candidate_at: float = 0.0
    last_verify_at: float = 0.0
    last_probe_at: float = 0.0
    current_offset: float = 0.0
    video_channel: str = ""
    audio_channel: str = ""
    candidate_recovery_hold: bool = False

    def locked(self):
        return self.state == ALIGN_STATE_ALIGNED

    def snapshot(self):
        return {
            "state": self.state,
            "message": self.message,
            "mismatch_count": self.mismatch_count,
            "checks": self.checks,
            "video_clock": self.video_clock,
            "audio_clock": self.audio_clock,
            "candidate_video_clock": self.candidate_video_clock,
            "candidate_audio_clock": self.candidate_audio_clock,
            "candidate_message": self.candidate_message,
            "candidate_video_seconds_back": self.candidate_video_seconds_back,
            "candidate_audio_seconds_back": self.candidate_audio_seconds_back,
            "verify_video_clock": self.verify_video_clock,
            "verify_audio_clock": self.verify_audio_clock,
            "verify_message": self.verify_message,
            "verify_delta": self.verify_delta,
            "verify_video_seconds_back": self.verify_video_seconds_back,
            "verify_audio_seconds_back": self.verify_audio_seconds_back,
            "retry_backoff_seconds": self.retry_backoff_seconds,
            "next_probe_at": self.next_probe_at,
            "current_offset": self.current_offset,
            "last_candidate_offset": self.last_candidate_offset,
            "candidate_recovery_hold": self.candidate_recovery_hold,
        }


class AutoAlignController:
    def __init__(
        self,
        manager: Any,
        video: Any,
        audio: Any,
        profile: dict,
        pipeline_video: Any,
        pipeline_audio: Any,
        current_pipeline: Any,
        mux: Any,
        handoff_deferred_type: type[BaseException],
    ):
        self.manager = manager
        self.video = video
        self.audio = audio
        self.pipeline_video = pipeline_video
        self.pipeline_audio = pipeline_audio
        self.current_pipeline = current_pipeline
        self.mux = mux
        self.profile = profile.copy()
        self.handoff_deferred_type = handoff_deferred_type
        self.run_id = 0
        self.first_segment_deadline = 0.0

        self.monitor = AlignmentMonitor()
        self.monitor.video_channel = video.name if video else ""
        self.monitor.audio_channel = audio.name if audio and audio.url else ""
        self.monitor.current_offset = float(profile.get("offset_seconds", 0) or 0)
        self.manager.auto_align_publish_status(self.monitor)

    def refresh_profile(self):
        self.profile = self.manager.auto_align_refresh_profile(self.profile)
        return self.profile

    def set_state(self, state: str, message: str, *, now_ts: float | None = None):
        self.monitor.state = state
        self.monitor.message = message
        if now_ts is not None:
            self.monitor.last_probe_at = now_ts

    def schedule_probe(self, delay_seconds: float, *, now_ts: float | None = None):
        base = time.time() if now_ts is None else now_ts
        self.monitor.next_probe_at = base + max(0.0, float(delay_seconds or 0))

    def publish_status(self, message: str | None = None):
        self.manager.auto_align_publish_status(self.monitor, message)

    def log(self, message: str):
        self.manager.auto_align_log(message)

    def read_probe_clocks(self, video_frame, audio_frame):
        return self.manager.auto_align_read_probe_clocks(video_frame, audio_frame, self.profile)

    def collect_probe_readings(self, sample_count: int, timeout: int, spacing_seconds: float, *, stage: str):
        return self.manager.auto_align_collect_probe_readings(
            self.pipeline_video,
            self.pipeline_audio,
            self.profile,
            sample_count,
            timeout,
            spacing_seconds,
            stage=stage,
        )

    def pair_capture_skew(self, video_cap, audio_cap):
        return self.manager.auto_align_pair_capture_skew(video_cap, audio_cap)

    def handoff_candidate(self, next_profile: dict):
        return self.manager.auto_align_handoff_candidate(
            self.pipeline_video,
            self.pipeline_audio,
            next_profile,
            self.current_pipeline,
            self.mux,
            f"run_{self.run_id:03d}",
        )

    def persist_offset(self, offset: float):
        self.manager.auto_align_persist_offset(offset)

    def after_handoff(self):
        self.manager.auto_align_after_handoff(self.current_pipeline, self.profile)

    def handoff_failure(self, message: str):
        self.manager.auto_align_handoff_failure(message)

    def alignment_backoff_seconds(self, failures: int):
        if failures <= 0:
            return 0
        value = ALIGN_RETRY_BACKOFF_BASE_SECONDS * (2 ** max(0, failures - 1))
        return int(min(ALIGN_RETRY_BACKOFF_MAX_SECONDS, value))

    def probe_interval(self):
        base = float(self.profile.get("auto_align_interval", 0) or 0)
        return max(base, 5.0)

    def accept_candidate(self, candidate: float | None, current: float):
        if candidate is None:
            return False, "candidate missing"
        max_offset = float(self.profile.get("auto_align_max_offset", 0) or 0)
        if max_offset < 1:
            max_offset = 1.0
        if abs(candidate) > max_offset:
            return False, f"candidate {candidate:.3f}s exceeds +/-{max_offset}s"
        if abs(candidate - current) <= 1.0:
            return False, f"delta {abs(candidate-current):.3f}s <= 1.000s"
        threshold = float(self.profile.get("auto_align_threshold", 0) or 0)
        if threshold < 0.1:
            threshold = 0.1
        if abs(candidate - current) < threshold:
            return False, f"delta {abs(candidate-current):.3f}s < threshold {threshold}s"
        return True, f"candidate {candidate:.3f}s accepted"

    def sample_pair_offset(self, video_sample, audio_sample):
        offset = -(audio_sample.game_time - video_sample.game_time)
        max_offset = float(self.profile.get("auto_align_max_offset", 0) or 0)
        if max_offset < 1:
            max_offset = 1.0
        if abs(offset) > max_offset:
            return None
        return offset

    def analyze_probe_readings(self, readings):
        self.monitor.current_offset = float(self.profile.get("offset_seconds", 0) or 0)
        self.monitor.checks += 1

        if not readings:
            self.monitor.mismatch_count = 1
            self.monitor.message = "ocr unstable: no readings"
            self.monitor.candidate_video_clock = ""
            self.monitor.candidate_audio_clock = ""
            self.monitor.candidate_message = self.monitor.message
            return None, self.monitor.message

        video_sample, audio_sample = readings[0]
        self.monitor.video_missing_count = 0 if video_sample else 1
        self.monitor.audio_missing_count = 0 if audio_sample else 1
        self.monitor.video_clock = video_sample.text if video_sample else ""
        self.monitor.audio_clock = audio_sample.text if audio_sample else ""
        self.monitor.candidate_video_clock = self.monitor.video_clock
        self.monitor.candidate_audio_clock = self.monitor.audio_clock

        if not video_sample or not audio_sample:
            self.monitor.mismatch_count = 1
            self.monitor.message = "ocr unstable: candidate frame missing clock"
            self.monitor.candidate_message = self.monitor.message
            return None, self.monitor.message

        candidate = self.sample_pair_offset(video_sample, audio_sample)
        if candidate is None:
            self.monitor.mismatch_count = 1
            self.monitor.message = "candidate exceeds max offset"
            self.monitor.candidate_message = self.monitor.message
            return None, self.monitor.message

        self.monitor.mismatch_count = 0
        self.monitor.last_candidate_offset = candidate
        self.monitor.last_candidate_at = time.time()
        self.monitor.message = (
            f"candidate {candidate:.3f}s "
            f"v={video_sample.text} a={audio_sample.text}"
        )
        self.monitor.candidate_message = self.monitor.message
        self.log(f"auto-align: {self.monitor.message}")
        return candidate, self.monitor.message

    def register_failure(self, message: str, *, now_ts: float | None = None, state: str | None = None):
        self.monitor.consecutive_failures += 1
        self.monitor.retry_backoff_seconds = self.alignment_backoff_seconds(self.monitor.consecutive_failures)
        self.monitor.candidate_recovery_hold = True
        delay = self.monitor.retry_backoff_seconds or self.probe_interval()
        self.schedule_probe(delay, now_ts=now_ts)
        self.set_state(state or ALIGN_STATE_WAITING, message, now_ts=now_ts)

    def register_success(self, offset: float, *, now_ts: float | None = None):
        self.monitor.consecutive_failures = 0
        self.monitor.retry_backoff_seconds = 0
        self.monitor.candidate_recovery_hold = False
        self.monitor.last_successful_offset = offset
        self.monitor.current_offset = offset
        self.monitor.mismatch_count = 0
        self.schedule_probe(self.probe_interval(), now_ts=now_ts)
        self.set_state(ALIGN_STATE_ALIGNED, f"aligned at {offset:.3f}s", now_ts=now_ts)

    def maybe_probe(self, *, now_ts: float, mtime: float | None, timeout: int):
        if self.monitor.next_probe_at <= 0:
            self.schedule_probe(self.probe_interval(), now_ts=now_ts)
        if not mtime or now_ts < self.monitor.next_probe_at:
            self.publish_status()
            return None

        self.set_state(ALIGN_STATE_PROBING, "capturing probe frames", now_ts=now_ts)
        self.monitor.verify_video_clock = ""
        self.monitor.verify_audio_clock = ""
        self.monitor.verify_message = ""
        self.monitor.verify_delta = None
        self.monitor.verify_video_seconds_back = None
        self.monitor.verify_audio_seconds_back = None
        self.publish_status()
        try:
            readings, error = self.collect_probe_readings(
                ALIGN_CANDIDATE_SAMPLE_COUNT,
                timeout,
                ALIGN_SAMPLE_SPACING_SECONDS,
                stage="candidate",
            )
        except Exception as exc:
            error = str(exc).strip()
            readings = []
        if error:
            detail = str(error).strip()
            if detail.startswith("frame source missing:") or detail.startswith("source cache unavailable:"):
                message = f"source cache unavailable: {detail.removeprefix('source cache unavailable:').strip()}"
            else:
                message = f"frame capture failed: {detail}" if not detail.startswith("frame capture failed:") else detail
            self.register_failure(message, now_ts=now_ts, state=ALIGN_STATE_CAPTURE_FAILED)
            self.log(f"auto-align: {self.monitor.message}; keeping current offset")
            self.publish_status()
            return None

        new_off, a_msg = self.analyze_probe_readings(readings)
        accepted, accept_msg = self.accept_candidate(
            new_off,
            float(self.profile.get("offset_seconds", 0) or 0),
        )
        if new_off is not None and accepted and self.monitor.candidate_recovery_hold:
            self.monitor.candidate_recovery_hold = False
            self.schedule_probe(self.probe_interval(), now_ts=now_ts)
            hold_msg = f"{a_msg}; recovery hold after OCR failure"
            self.log(f"auto-align: {hold_msg}")
            self.set_state(ALIGN_STATE_WAITING, hold_msg, now_ts=now_ts)
            self.publish_status()
            return None
        if new_off is None or not accepted:
            self.schedule_probe(self.probe_interval(), now_ts=now_ts)
            self.set_state(ALIGN_STATE_WAITING, a_msg if new_off is None else accept_msg, now_ts=now_ts)
            self.publish_status()
            return None

        self.set_state(ALIGN_STATE_PROBING, f"{a_msg}; handoff candidate {new_off:.3f}s", now_ts=now_ts)
        self.publish_status()
        next_profile = self.profile.copy()
        next_profile["offset_seconds"] = new_off
        try:
            self.run_id += 1
            self.current_pipeline, self.mux = self.handoff_candidate(next_profile)
            self.profile = next_profile
            self.after_handoff()
            self.persist_offset(new_off)
            self.register_success(new_off, now_ts=now_ts)
            self.first_segment_deadline = time.time() + timeout
            self.publish_status(f"{a_msg}; handoff complete")
        except Exception as exc:
            msg = f"handoff failed: {exc}; keeping current stream"
            self.log(msg)
            self.register_failure(msg, now_ts=now_ts, state=ALIGN_STATE_WAITING)
            self.handoff_failure(msg)
            if not isinstance(exc, self.handoff_deferred_type):
                return f"handoff failed: {exc}"
        self.publish_status()
        return None
