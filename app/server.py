#!/usr/bin/env python3
import hashlib
import json
import contextlib
import math
import mimetypes
import os
import errno
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, unquote_plus, urlencode, urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo
from typing import Any

import cv2
import numpy as np


def app_root():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


APP_ROOT = app_root()
if getattr(sys, "frozen", False):
    APP_DIR = Path(getattr(sys, "_MEIPASS", APP_ROOT)) / "app"
else:
    APP_DIR = APP_ROOT / "app"
STATIC_DIR = APP_DIR / "static"
BIN_DIR = APP_ROOT / "bin"
if BIN_DIR.exists():
    os.environ["PATH"] = str(BIN_DIR) + os.pathsep + os.environ.get("PATH", "")
RUNTIME_DIR = Path(os.environ.get("LIVE_SYNC_RUNTIME_DIR", str(APP_ROOT / "data")))
STATE_DIR = Path(os.environ.get("LIVE_SYNC_STATE_DIR") or os.environ.get("STATE_DIR") or str(RUNTIME_DIR / "state"))
HLS_DIR = Path(os.environ.get("HLS_DIR") or os.environ.get("LIVE_SYNC_HLS_DIR") or str(RUNTIME_DIR / "hls"))
WORK_DIR = Path(os.environ.get("WORK_DIR") or str(RUNTIME_DIR / "work"))
PORT = int(os.environ.get("PORT", "18080"))
OFFSET_STATE = Path(os.environ.get("OFFSET_STATE", str(STATE_DIR / "last_sync_offset.json")))

PROFILE_PATH = STATE_DIR / "profile.json"
ROI_PATH = STATE_DIR / "roi.json"
SNAPSHOT_DIR = STATE_DIR / "snapshots"
ROI_PREVIEW_DIR = STATE_DIR / "roi_previews"
RECORDING_DIR = STATE_DIR / "recordings"
SNAPSHOT_KINDS = ("cache_video", "cache_audio", "video", "audio")
OCR_NO_SCOREBOARD = object()
CLOCK_RE = re.compile(r"(?<!\d)([0-9]{1,3})[:：.]([0-9]{2})(?!\d)")
# Matches XX:YY+ZZ (e.g. 4:32+8, 94:32+8) - clock with stoppage minutes appended
CLOCK_WITH_ADDED_RE = re.compile(r"(?<!\d)([0-9]{1,3})[:：.]([0-9]{2})\s*\+([0-9]{1,2})(?:\s*min?s?\.?)?(?!\d)")
# Matches XX:00+YY:ZZ (e.g. 45:00+02:30) - traditional stoppage
STOPPAGE_RE = re.compile(r"(?<!\d)([0-9]{1,3})(?::00)?\+([0-9]{1,2})(?:[:：.]([0-9]{2})(?!\d)|(?![:：.\d]))")
# Matches XX mins.+YY (e.g. 4:32 mins.+8) - separate stoppage text
STOPPAGE_SEPARATE_RE = re.compile(r"(?<!\d)([0-9]{1,3})[:：.]([0-9]{2})\s+(?:min?s?\.?\s*)?\+([0-9]{1,2})(?!\d)")
STOPPAGE_BASE_RE = re.compile(r"(?<!\d)([0-9]{1,3})(?:[:：.]([0-9]{2}))?(?!\d)")
ADDED_TIME_RE = re.compile(r"(?<!\d)\+([0-9]{1,2})(?:[:：.]([0-9]{2})(?!\d)|(?![:：.\d]))")
ADDED_LINE_RE = re.compile(r"^\+?([0-9]{1,2})(?:[:：.]([0-9]{2}))?$")
ELAPSED_ADDED_TIME_RE = re.compile(r"(?<![+\d])([0-9]{1,2})[:：.]([0-5][0-9])(?:\+[0-9]{1,2})?(?!\d)")
ADJACENT_STOPPAGE_BASES = {45, 90, 105, 120}
HLS_MAP_URI_RE = re.compile(r'URI="([^"]+)"')
DEFAULT_VIDEO_TIMER_ROI_PRESETS = "\n".join([
    "0.132,0.055,0.078,0.140",
    "0.333,0.058,0.080,0.140",
    "0.114,0.049,0.077,0.077",
    "0.111,0.000,0.077,0.185",
])
DEFAULT_AUDIO_TIMER_ROI_PRESETS = "0.824,0.080,0.078,0.140"
TIMER_ROI_SCAN_INTERVAL_SECONDS = 60
TIMER_ROI_SCAN_WINDOW_SECONDS = 300
TIMER_ROI_PREVIEW_MULTIPLIER = 9.0
HLS_RECOVERY_WINDOW_SECONDS = int(os.environ.get("HLS_RECOVERY_WINDOW_SECONDS", "300") or 300)
HLS_CLIENT_GRACE_SECONDS = int(os.environ.get("HLS_CLIENT_GRACE_SECONDS", "240") or 240)


def env(name, default=""):
    return os.environ.get(name, default)


def env_list(name):
    return [item.strip() for item in env(name).replace("\n", ",").split(",") if item.strip()]


def env_multiline_list(name):
    return "\n".join(env_list(name))


def split_lines(value):
    return [item.strip() for item in str(value or "").replace("\r", "\n").split("\n") if item.strip()]


def m3u_sources(urls="", local_text="", local_label="本地 M3U"):
    sources = []
    if str(local_text or "").strip():
        local_id = re.sub(r"[^a-z0-9_-]+", "-", normalize(local_label)).strip("-") or "m3u"
        sources.append({"url": f"local://{local_id}", "text": str(local_text), "label": local_label})
    for idx, url in enumerate(split_lines(urls), start=1):
        sources.append({"url": url, "text": "", "label": f"M3U {idx}"})
    return sources


def prefer_garyshare_4k_sources(sources, channel_name):
    ordered = list(sources or [])
    if "4k" not in normalize(channel_name):
        return ordered
    if not any("garyshare" in str(source.get("url", "")).lower() for source in ordered):
        return ordered
    return sorted(
        ordered,
        key=lambda source: 0 if "garyshare" in str(source.get("url", "")).lower() else 1,
    )


def selected_audio_channels(profile):
    channels = []
    primary = str(profile.get("audio_channel", "") or "").strip()
    if primary:
        channels.append(primary)
    for channel in profile.get("audio_fallbacks", []) or []:
        text = str(channel).strip()
        if text and text not in channels:
            channels.append(text)
    return channels


def parse_header_lines(value):
    headers = {}
    text = str(value or "").replace("\\n", "\n").replace("\r", "\n")
    for raw in text.split("\n"):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if ":" in line:
            key, val = line.split(":", 1)
        elif "=" in line:
            key, val = line.split("=", 1)
        else:
            continue
        key = normalize_header_name(key)
        val = val.strip()
        if key and val:
            headers[key] = val
    return headers


def normalize_header_name(name):
    aliases = {
        "user-agent": "User-Agent",
        "user_agent": "User-Agent",
        "referer": "Referer",
        "referrer": "Referer",
        "http-referrer": "Referer",
        "origin": "Origin",
        "cookie": "Cookie",
        "authorization": "Authorization",
    }
    raw = str(name or "").strip()
    lowered = raw.lower()
    if lowered in aliases:
        return aliases[lowered]
    return "-".join(part[:1].upper() + part[1:].lower() for part in raw.split("-") if part)


def parse_header_blob(text):
    headers = {}
    for raw in re.split(r"[&;\r\n]+", str(text or "")):
        part = raw.strip()
        if not part:
            continue
        if "=" in part:
            key, val = part.split("=", 1)
        elif ":" in part:
            key, val = part.split(":", 1)
        else:
            continue
        name = normalize_header_name(unquote_plus(key))
        value = unquote_plus(val).strip()
        if name and value:
            headers[name] = value
    return headers


def parse_pipe_headers(text):
    return parse_header_blob(text)


def split_url_headers(url):
    text = str(url or "").strip()
    if "|" not in text:
        return text, {}
    base, header_text = text.split("|", 1)
    return base.strip(), parse_pipe_headers(header_text)


def env_bool(name, default=False):
    raw = env(name, "1" if default else "0").strip().lower()
    return raw not in ("0", "false", "no", "off", "")


def load_offset_default():
    manual = env("SYNC_OFFSET").strip()
    if manual:
        return float(manual)
    try:
        with OFFSET_STATE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return float(data["offset_seconds"])
    except (FileNotFoundError, OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        pass
    return float(env("DEFAULT_OFFSET", "29") or 0)


def make_default_profile():
    auto_align_interval = int(env("AUTO_ALIGN_INTERVAL", env("SNAPSHOT_INTERVAL", "60")) or 60)
    return {
        "name": env("PROFILE_NAME", "4K + Chinese commentary"),
        "video_url": "",
        "audio_url": "",
        "video_headers": env("VIDEO_HEADERS", ""),
        "audio_headers": env("AUDIO_HEADERS", ""),
        "video_playlist": env_multiline_list("VIDEO_M3U_URL"),
        "video_local_m3u": "",
        "video_primary": env("VIDEO_CHANNEL_NAME", ""),
        "video_fallbacks": env_list("FALLBACK_VIDEO_CHANNELS"),
        "audio_playlist": env_multiline_list("AUDIO_M3U_URL"),
        "audio_local_m3u": "",
        "audio_channel": env("AUDIO_CHANNEL_NAME", ""),
        "audio_fallbacks": env_list("FALLBACK_AUDIO_CHANNELS"),
        "offset_seconds": load_offset_default(),
        "retry_limit": int(env("RETRY_LIMIT", "3") or 3),
        "timeout_seconds": int(env("TIMEOUT_SECONDS", "25") or 25),
        "segment_time": float(env("SEGMENT_TIME", "4") or 4),
        "playlist_size": int(env("PLAYLIST_SIZE", "60") or 60),
        "hls_segment_type": env("HLS_SEGMENT_TYPE", "auto").lower(),
        "local_cache_enabled": env_bool("LOCAL_CACHE_ENABLED", True),
        "local_cache_seconds": int(env("LOCAL_CACHE_SECONDS", "96") or 96),
        "public_base_url": env("PUBLIC_BASE_URL", ""),
        "channel_id": env("CHANNEL_ID", "cctv5-4k-cn"),
        "channel_name": env("CHANNEL_NAME", "CCTV5 4K Chinese"),
        "channel_number": env("CHANNEL_NUMBER", "5"),
        "channel_group": env("CHANNEL_GROUP", "Sports"),
        "auto_align_enabled": False,
        "auto_align_interval": auto_align_interval,
        "auto_align_samples": int(env("AUTO_ALIGN_SAMPLES", "3") or 3),
        "auto_align_step": float(env("AUTO_ALIGN_STEP", "1") or 1),
        "auto_align_threshold": float(env("AUTO_ALIGN_THRESHOLD", "1") or 1),
        "auto_align_max_offset": float(env("AUTO_ALIGN_MAX_OFFSET", "180") or 180),
        "auto_align_relocate_attempts": int(env("AUTO_ALIGN_RELOCATE_ATTEMPTS", "3") or 3),
        "auto_align_debug_override": env_bool("AUTO_ALIGN_DEBUG_OVERRIDE", False),
        "snapshot_interval": auto_align_interval,
        "video_roi": env("VIDEO_TIMER_ROI", "0.050,0.050,0.070,0.050"),
        "audio_roi": env("AUDIO_TIMER_ROI", "0.885,0.085,0.075,0.060"),
        "video_roi_presets": env("VIDEO_TIMER_ROI_PRESETS", "").strip() or DEFAULT_VIDEO_TIMER_ROI_PRESETS,
        "audio_roi_presets": env("AUDIO_TIMER_ROI_PRESETS", "").strip() or DEFAULT_AUDIO_TIMER_ROI_PRESETS,
        "schedule_enabled": env_bool("SCHEDULE_ENABLED", True),
        "schedule_provider": env("SCHEDULE_PROVIDER", "espn"),
        "schedule_league": env("SCHEDULE_LEAGUE", "fifa.world"),
        "schedule_timezone": env("SCHEDULE_TIMEZONE", "Asia/Shanghai"),
        "schedule_refresh_hours": int(env("SCHEDULE_REFRESH_HOURS", "24") or 24),
        "schedule_poll_seconds": int(env("SCHEDULE_POLL_SECONDS", "60") or 60),
        "schedule_pre_minutes": int(env("SCHEDULE_PRE_MINUTES", "10") or 10),
        "schedule_duration_minutes": int(env("SCHEDULE_DURATION_MINUTES", "150") or 150),
        "schedule_post_minutes": int(env("SCHEDULE_POST_MINUTES", "20") or 20),
        "ocr_provider": normalize_ocr_provider(env("OCR_PROVIDER", "")),
        "ocr_api_key": env("OCR_API_KEY", "").strip(),
        "ocrspace_api_key": env("OCRSPACE_API_KEY", "").strip(),
        "ocr_custom_endpoint": env("OCR_CUSTOM_ENDPOINT", "").strip(),
        "ocr_custom_model": env("OCR_CUSTOM_MODEL", "gpt-4o").strip(),
    }

def now():
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def normalize(value):
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def json_load(path, default):
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except (OSError, json.JSONDecodeError):
        return default


def json_save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=True, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def parse_bool(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in ("0", "false", "no", "off", "")


def coerce_int(value, default, minimum=None):
    try:
        if value in ("", None):
            return int(default)
        result = int(value)
    except (TypeError, ValueError):
        return int(default)
    if minimum is not None and result < minimum:
        return max(int(default), int(minimum))
    return result


def coerce_float(value, default, minimum=None):
    try:
        if value in ("", None):
            return float(default)
        result = float(value)
    except (TypeError, ValueError):
        return float(default)
    if minimum is not None and result < minimum:
        return max(float(default), float(minimum))
    return result


def effective_segment_time(profile):
    return max(coerce_float(profile.get("segment_time"), DEFAULT_PROFILE["segment_time"], minimum=0.5), 4.0)


def hls_stall_timeout(profile):
    input_timeout = coerce_int(profile.get("timeout_seconds"), DEFAULT_PROFILE["timeout_seconds"], minimum=5)
    segment_seconds = effective_segment_time(profile)
    return max(input_timeout, int(math.ceil(segment_seconds * 4)))


def startup_hls_wait_timeout(profile):
    base = hls_stall_timeout(profile)
    offset = abs(coerce_float(profile.get("offset_seconds"), DEFAULT_PROFILE["offset_seconds"]))
    segment_seconds = effective_segment_time(profile)
    extra = max(0, int(math.ceil(offset + segment_seconds * 1.5))) if offset >= 0.5 else 0
    return base + extra


def hls_segment_type(profile):
    value = str(profile.get("hls_segment_type", DEFAULT_PROFILE.get("hls_segment_type", "auto")) or "auto").strip().lower()
    if value in ("ts", "mpegts"):
        return "mpegts"
    if value in ("fmp4", "mp4"):
        return "fmp4"
    return "auto"


def hls_segment_ext(profile):
    return ".ts" if hls_segment_type(profile) == "mpegts" else ".m4s"


def effective_hls_segment_type(profile, prepared=None):
    configured = hls_segment_type(profile)
    if configured != "auto":
        return configured
    return "fmp4" if prepared and prepared.channel_prefers_fmp4 else "mpegts"


def effective_hls_segment_ext(profile, prepared=None):
    return ".ts" if effective_hls_segment_type(profile, prepared) == "mpegts" else ".m4s"


def proc_alive(pid):
    try:
        os.kill(int(pid), 0)
    except (ProcessLookupError, ValueError, TypeError, OSError):
        return False
    return True


def proc_cmdline(pid):
    try:
        raw = (Path("/proc") / str(int(pid)) / "cmdline").read_bytes()
    except (OSError, ValueError, TypeError):
        return ""
    return raw.replace(b"\x00", b" ").decode("utf-8", errors="ignore")


def kill_pid(pid, sig):
    try:
        os.kill(int(pid), sig)
        return True
    except (ProcessLookupError, ValueError, TypeError, OSError):
        return False


def strip_dovi_rpu():
    return env_bool("STRIP_DOVI_RPU", True)


def output_audio_codec():
    codec = env("OUTPUT_AUDIO_CODEC", "copy").strip().lower()
    return "copy" if codec == "copy" else "aac"


def ffmpeg_user_agent():
    return env("FFMPEG_USER_AGENT", "Emby") or "Emby"


def default_request_headers():
    raw = env("DEFAULT_REQUEST_HEADERS", "").strip()
    if not raw:
        raw = "User-Agent: Emby\nAccept: */*\nCache-Control: no-cache\nPragma: no-cache"
    headers = parse_header_lines(raw)
    if "User-Agent" not in headers:
        headers["User-Agent"] = ffmpeg_user_agent()
    return headers


def coerce_text(value, default=""):
    value = str(value or "").strip()
    return value if value else str(default)


def normalize_key_text(value):
    text = normalize(value)
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def parse_roi(value):
    parts = [float(x.strip()) for x in str(value or "").split(",")]
    if len(parts) != 4:
        raise ValueError("ROI must use x,y,width,height")
    x, y, w, h = parts
    if x < 0 or y < 0 or w <= 0 or h <= 0 or x + w > 1 or y + h > 1:
        raise ValueError("ROI values must be normalized between 0 and 1")
    return x, y, w, h


def format_roi(value):
    return ",".join(f"{part:.3f}" for part in parse_roi(value))


def parse_roi_list(value):
    rois = []
    seen = set()
    for line in split_lines(value):
        try:
            roi = parse_roi(line)
        except (TypeError, ValueError):
            continue
        key = tuple(round(part, 4) for part in roi)
        if key in seen:
            continue
        seen.add(key)
        rois.append(roi)
    return rois


OCR_PROVIDERS = {"ocrspace", "custom"}
OCR_PROVIDER_LABELS = {
    "ocrspace": "OCR.space",
    "custom": "自定义 OCR",
}
OCR_FALLBACK_COOLDOWN_SECONDS = int(os.environ.get("OCR_FALLBACK_COOLDOWN_SECONDS", "180") or 180)


def normalize_ocr_provider(value):
    provider = coerce_text(value).strip().lower()
    return provider if provider in OCR_PROVIDERS else ""


def ocr_provider_ready(profile):
    return any(ocr_provider_ready_for(provider, profile) for provider in ocr_provider_order(profile))


def ocr_provider_ready_for(provider, profile):
    provider = normalize_ocr_provider(provider)
    if not provider:
        return False
    key_name = "ocr_api_key" if provider == "custom" else "ocrspace_api_key"
    api_key = coerce_text(profile.get(key_name, "")).strip()
    if not api_key:
        return False
    if provider == "custom":
        endpoint = coerce_text(profile.get("ocr_custom_endpoint", "")).strip()
        return bool(endpoint)
    return True


def _other_provider(provider):
    return "ocrspace" if normalize_ocr_provider(provider) == "custom" else "custom"


def ocr_provider_order(profile):
    primary = normalize_ocr_provider(profile.get("ocr_provider"))
    if not primary:
        return []
    order = [primary]
    fallback = _other_provider(primary)
    if fallback != primary:
        order.append(fallback)
    return order


def ocr_provider_label(provider):
    return OCR_PROVIDER_LABELS.get(normalize_ocr_provider(provider), provider or "")


DEFAULT_PROFILE = make_default_profile()
DEFAULT_PROFILE["auto_align_enabled"] = ocr_provider_ready(DEFAULT_PROFILE)

URL_SAVE_KEYS = {
    "video_url",
    "audio_url",
    "public_base_url",
}
RUNTIME_AUTO_ALIGN_KEYS = {
    "auto_align_interval",
    "auto_align_samples",
    "auto_align_step",
    "auto_align_threshold",
    "auto_align_max_offset",
    "auto_align_relocate_attempts",
    "auto_align_debug_override",
    "video_roi",
    "audio_roi",
    "video_roi_presets",
    "audio_roi_presets",
    "schedule_enabled",
    "schedule_provider",
    "schedule_league",
    "schedule_timezone",
    "schedule_refresh_hours",
    "schedule_poll_seconds",
    "schedule_pre_minutes",
    "schedule_duration_minutes",
    "schedule_post_minutes",
}


def local_cache_enabled(profile):
    return parse_bool(profile.get("local_cache_enabled", DEFAULT_PROFILE.get("local_cache_enabled", True)))


def effective_local_cache_seconds(profile):
    segment = effective_segment_time(profile)
    configured = coerce_int(profile.get("local_cache_seconds"), DEFAULT_PROFILE["local_cache_seconds"], minimum=30)
    offset = abs(coerce_float(profile.get("offset_seconds"), DEFAULT_PROFILE["offset_seconds"]))
    # Keep enough cache for smooth handoff, but cap growth so long-running
    # sessions do not accumulate multi-hundred-MB source caches per side.
    return int(math.ceil(min(configured, max(30, min(offset + segment * 3, 120)))))


def local_cache_list_size(profile):
    return max(8, int(math.ceil(effective_local_cache_seconds(profile) / effective_segment_time(profile))) + 4)


def _strip_url_fields(profile):
    return {k: v for k, v in profile.items() if k not in URL_SAVE_KEYS}

def _redact_url(text):
    if not env_bool("LOG_REDACT_URLS", False):
        return str(text or "")
    return re.sub(r"https?://[^\s\"'<>)]+", "<URL>", str(text or ""))



def clear_directory_contents(path, *, keep=()):
    path.mkdir(parents=True, exist_ok=True)
    keep = set(keep)
    for item in path.iterdir():
        if item.name in keep:
            continue
        if item.is_dir() and not item.is_symlink():
            shutil.rmtree(item, ignore_errors=True)
        else:
            item.unlink(missing_ok=True)


def safe_child_path(root, relative_path):
    root = Path(root).resolve()
    target = (root / str(relative_path or "").lstrip("/")).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise PermissionError("path escapes served directory") from exc
    return target


@dataclass
class Channel:
    name: str
    url: str
    tvg_id: str = ""
    tvg_name: str = ""
    group: str = ""
    headers: dict = field(default_factory=dict)

    def searchable(self):
        return normalize(" ".join([self.name, self.tvg_name, self.tvg_id, self.group, self.url]))


@dataclass(frozen=True)
class ClockSample:
    media_time: float
    game_time: int
    text: str
    roi: tuple | None = None


def coerce_clock_sample(found, media_time=0.0):
    if not found:
        return None
    if isinstance(found, ClockSample):
        if found.media_time == media_time:
            return found
        return ClockSample(media_time, found.game_time, found.text, found.roi)
    return ClockSample(media_time, found[0], found[1], found[2])


@dataclass(frozen=True)
class ClockCandidate:
    diff: float
    offset: float
    video: ClockSample
    audio: ClockSample


@dataclass(frozen=True)
class FrameCaptureResult:
    path: Path
    kind: str
    started_at: float
    finished_at: float
    media_time: float = 0.0
    source: str = ""


class HandoffDeferred(RuntimeError):
    pass


@dataclass
class LocalSourceCache:
    run_dir: Path
    video: Channel
    audio: Channel
    video_proc: object | None = None
    audio_proc: object | None = None


@dataclass(frozen=True)
class PipelineFailure:
    reason: str
    kind: str = "unknown"

    def __str__(self):
        return self.reason


@dataclass
class PreparedPipeline:
    offset: float
    run_dir: Path
    video_input: list
    audio_input: list
    delay_procs: list
    audio_map: str = ""
    audio_copy_bsf: str = ""
    single_input_av: bool = False
    video_codec: str = ""
    compatible_mux: bool = False
    channel_prefers_fmp4: bool = False
    video_input_label: str = "direct video"
    audio_input_label: str = "direct audio"
    video_snapshot_input: list = field(default_factory=list)
    audio_snapshot_input: list = field(default_factory=list)
    snapshot_jobs: list = field(default_factory=list)


@dataclass(frozen=True)
class ClockParse:
    game_time: int
    text: str
    kind: str = "clock"


@dataclass
class AlignmentMonitor:
    video_clock: str = ""
    audio_clock: str = ""
    state: str = "acquiring"
    message: str = "waiting for OCR"
    mismatch_offsets: list = field(default_factory=list)
    mismatch_count: int = 0
    checks: int = 0
    video_missing_count: int = 0
    audio_missing_count: int = 0

    def locked(self):
        return True

    def snapshot(self):
        return {
            "state": self.state,
            "message": self.message,
            "mismatch_count": self.mismatch_count,
            "checks": self.checks,
            "video_clock": self.video_clock,
            "audio_clock": self.audio_clock,
        }


@dataclass(frozen=True)
class MatchEvent:
    event_id: str
    name: str
    short_name: str
    start_ts: float
    end_ts: float
    state: str = ""
    completed: bool = False


@dataclass
class RecordingSession:
    session_id: str
    label: str
    status: str = "starting"
    started_at: str = ""
    started_at_unix: float = 0.0
    stopped_at: str = ""
    stopped_at_unix: float = 0.0
    source_playlist: str = "/index.m3u8"
    source_segment_type: str = "mpegts"
    segment_time: float = 4.0
    playlist_path: str = ""
    merged_path: str = ""
    merge_status: str = ""
    merge_message: str = ""
    error: str = ""
    pid: int | None = None
    segment_count: int = 0
    last_update_at: str = ""
    last_update_unix: float = 0.0

    def as_dict(self):
        return {
            "session_id": self.session_id,
            "label": self.label,
            "status": self.status,
            "started_at": self.started_at,
            "started_at_unix": self.started_at_unix,
            "stopped_at": self.stopped_at,
            "stopped_at_unix": self.stopped_at_unix,
            "source_playlist": self.source_playlist,
            "source_segment_type": self.source_segment_type,
            "segment_time": self.segment_time,
            "playlist_path": self.playlist_path,
            "playlist_url": f"/recordings/{self.session_id}/recording.m3u8" if self.playlist_path else "",
            "merged_path": self.merged_path,
            "merged_url": f"/recordings/{self.session_id}/{Path(self.merged_path).name}" if self.merged_path else "",
            "merge_status": self.merge_status,
            "merge_message": self.merge_message,
            "error": self.error,
            "pid": self.pid,
            "segment_count": self.segment_count,
            "last_update_at": self.last_update_at,
            "last_update_unix": self.last_update_unix,
        }


class M3UResolver:
    attr_re = re.compile(r'([\w-]+)="([^"]*)"')

    def __init__(self, log):
        self.log = log
        self.cache = {}
        self.lock = threading.Lock()

    def fetch(self, url, force=False):
        if str(url or "").startswith("local://"):
            raise RuntimeError("local M3U source requires inline content")
        with self.lock:
            cached = self.cache.get(url)
            if cached and not force and time.time() - cached["time"] < 60:
                return cached["channels"]

        req = urllib.request.Request(url, headers={"User-Agent": "live-sync-webui/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            text = resp.read().decode("utf-8", errors="replace")
        channels = self.parse(text, base_url=url)

        with self.lock:
            self.cache[url] = {"time": time.time(), "channels": channels}
        self.log(f"loaded {len(channels)} channels from {_redact_url(url)}")
        return channels

    def fetch_source(self, source, force=False):
        url = str((source or {}).get("url", "") or "").strip()
        text = str((source or {}).get("text", "") or "")
        label = str((source or {}).get("label", "") or url or "local M3U").strip()
        if text:
            channels = self.parse(text, base_url=url if url and not url.startswith("local://") else "")
            self.log(f"loaded {len(channels)} channels from {label}")
            return channels
        return self.fetch(url, force=force)

    def parse(self, text, base_url=""):
        channels = []
        pending = {}
        pending_headers = {}
        for raw in text.splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.startswith("#EXTINF"):
                attrs = dict(self.attr_re.findall(line))
                display = line.split(",", 1)[1].strip() if "," in line else ""
                pending = {
                    "name": attrs.get("tvg-name") or display,
                    "tvg_id": attrs.get("tvg-id", ""),
                    "tvg_name": attrs.get("tvg-name", ""),
                    "group": attrs.get("group-title", ""),
                }
                pending_headers = {}
                continue
            lower = line.lower()
            if lower.startswith("#extvlcopt:"):
                option = line.split(":", 1)[1].strip()
                if "=" in option:
                    key, value = option.split("=", 1)
                    key = key.strip().lower()
                    if key in ("http-user-agent", "user-agent"):
                        pending_headers["User-Agent"] = value.strip()
                    elif key in ("http-referrer", "http-referer", "referer", "referrer"):
                        pending_headers["Referer"] = value.strip()
                    elif key in ("http-origin", "origin"):
                        pending_headers["Origin"] = value.strip()
                    elif key in ("http-cookie", "cookie"):
                        pending_headers["Cookie"] = value.strip()
                    elif key in ("http-header", "headers"):
                        pending_headers.update(parse_header_blob(value))
                continue
            if lower.startswith("#kodiprop:"):
                option = line.split(":", 1)[1].strip()
                if "=" in option:
                    key, value = option.split("=", 1)
                    key = key.strip().lower()
                    if key.endswith("stream_headers") or key.endswith("manifest_headers"):
                        pending_headers.update(parse_header_blob(value))
                    elif key.endswith("user-agent"):
                        pending_headers["User-Agent"] = value.strip()
                continue
            if lower.startswith("#exthttp:"):
                pending_headers.update(parse_header_blob(line.split(":", 1)[1]))
                continue
            if line.startswith("#"):
                continue
            name = pending.get("name") or line
            raw_url, inline_headers = split_url_headers(line)
            stream_url = urljoin(base_url, raw_url)
            headers = {**pending_headers, **inline_headers}
            channels.append(Channel(
                url=stream_url,
                name=name,
                tvg_id=pending.get("tvg_id", ""),
                tvg_name=pending.get("tvg_name", ""),
                group=pending.get("group", ""),
                headers=headers,
            ))
            pending = {}
            pending_headers = {}
        return channels

    def find(self, playlist_url, channel_name, force=False):
        target = normalize(channel_name)
        if not target:
            raise RuntimeError("empty channel name")
        channels = self.fetch(playlist_url, force=force)

        def names(ch):
            return [normalize(ch.name), normalize(ch.tvg_name), normalize(ch.tvg_id)]

        for ch in channels:
            if target in names(ch):
                return ch
        for ch in channels:
            if any(target in name or name in target for name in names(ch) if name):
                return ch
        raise RuntimeError(f"channel not found: {channel_name}")

    def find_any(self, playlist_urls, channel_name, force=False):
        return self.find_any_sources(m3u_sources(playlist_urls), channel_name, force=force)

    def find_any_sources(self, sources, channel_name, force=False):
        sources = list(sources or [])
        if not sources:
            raise RuntimeError("empty playlist URL")
        target = normalize(channel_name)
        if not target:
            raise RuntimeError("empty channel name")
        errors = []
        for idx, source in enumerate(sources, start=1):
            try:
                channels = self.fetch_source(source, force=force)

                def names(ch):
                    return [normalize(ch.name), normalize(ch.tvg_name), normalize(ch.tvg_id)]

                channel = None
                for ch in channels:
                    if target in names(ch):
                        channel = ch
                        break
                if channel is None:
                    for ch in channels:
                        if any(target in name or name in target for name in names(ch) if name):
                            channel = ch
                            break
                if channel is None:
                    raise RuntimeError(f"channel not found: {channel_name}")
                if len(sources) > 1:
                    group = channel.group or f"M3U {idx}"
                    return Channel(
                        name=channel.name,
                        url=channel.url,
                        tvg_id=channel.tvg_id,
                        tvg_name=channel.tvg_name,
                        group=f"{group} · M3U {idx}",
                        headers=dict(channel.headers),
                    )
                return channel
            except Exception as exc:
                errors.append(f"M3U {idx}: {exc}")
        raise RuntimeError("; ".join(errors) if errors else f"channel not found: {channel_name}")

    def search(self, playlist_url, query="", force=False, limit=2000):
        channels = self.fetch(playlist_url, force=force)
        q = normalize(query)
        if q:
            channels = [ch for ch in channels if q in ch.searchable()]
        return [asdict(ch) for ch in channels[:limit]]

    def search_any(self, playlist_urls, query="", force=False, limit=2000):
        return self.search_any_sources(m3u_sources(playlist_urls), query=query, force=force, limit=limit)

    def search_any_sources(self, sources, query="", force=False, limit=2000):
        sources = list(sources or [])
        channels = []
        errors = []
        for idx, source in enumerate(sources, start=1):
            try:
                for channel in self.fetch_source(source, force=force):
                    group = channel.group or f"M3U {idx}"
                    channels.append(Channel(
                        name=channel.name,
                        url=channel.url,
                        tvg_id=channel.tvg_id,
                        tvg_name=channel.tvg_name,
                        group=f"{group} · M3U {idx}",
                        headers=dict(channel.headers),
                    ))
            except Exception as exc:
                errors.append(f"M3U {idx}: {exc}")
                self.log(f"playlist preview failed for M3U {idx}: {_redact_url(exc)}")
        q = normalize(query)
        if q:
            channels = [ch for ch in channels if q in ch.searchable()]
        return {
            "channels": [asdict(ch) for ch in channels[:limit]],
            "errors": errors,
        }


class LiveManager:
    def __init__(self):
        self.logs = deque(maxlen=500)
        self.lock = threading.RLock()
        self.resolver = M3UResolver(self.log)
        self.profile = json_load(PROFILE_PATH, DEFAULT_PROFILE.copy())
        self.profile = self.normalize_profile(self.profile, save=False)
        if PROFILE_PATH.exists():
            json_save(PROFILE_PATH, _strip_url_fields(self.profile))
        self.roi = json_load(ROI_PATH, {})
        self.thread = None
        self.stop_event = threading.Event()
        self.processes = []
        self.process_tails = {}
        self.current_snapshot_jobs = []
        self.snapshot_lock = threading.Lock()
        self.snapshot_file_lock = threading.Lock()
        self.ocr_lock = threading.Lock()
        self.schedule_thread = None
        self.schedule_stop_event = threading.Event()
        self.schedule_events = []
        self.schedule_last_refresh = 0.0
        self.schedule_last_active_id = ""
        self.schedule_owned_run = False
        self.manual_override_until_event_id = ""
        self.recording_lock = threading.RLock()
        self.recording_thread = None
        self.recording_stop_event = threading.Event()
        self.recording_proc = None
        self.recording_session = None
        self.active_hls_playlist = HLS_DIR / "index.m3u8"
        self.hls_preserved_assets = deque(maxlen=16)
        self.managed_pidfile = STATE_DIR / "live_manager.pid"
        self.restart_request_id = 0
        self.ocr_provider_cooldowns = {}
        self.status = {
            "running": False,
            "stage": "stopped",
            "active_channel": "",
            "active_url": "",
            "active_audio_channel": "",
            "audio_url": "",
            "failure_count": 0,
            "last_error": "",
            "last_resolution": "",
            "last_segment_at": None,
            "started_at": None,
            "offset_seconds": self.profile.get("offset_seconds", 0),
            "auto_align_enabled": ocr_provider_ready(self.profile),
            "auto_align_msg": "",
            "auto_align_state": "idle",
            "auto_align_monitor": {},
            "last_alignment": None,
            "auto_align_offset_seconds": None,
            "last_snapshot_at": None,
            "last_ocr_results": {kind: None for kind in SNAPSHOT_KINDS},
            "ocr_request_count": 0,
            "ocr_request_last_at": None,
            "ocr_request_last_provider": "",
            "first_alignment_at": None,
            "first_alignment_ocr_request_count": None,
            "schedule": {
                "enabled": parse_bool(self.profile.get("schedule_enabled", DEFAULT_PROFILE.get("schedule_enabled", True))),
                "active": False,
                "active_match": None,
                "next_match": None,
                "last_refresh": None,
                "message": "",
                "manual_override": False,
            },
            "recording": {
                "enabled": False,
                "active": None,
            },
        }
        self._reclaim_runtime_processes()

    def log(self, message):
        line = f"[{now()}] {message}"
        with self.lock:
            self.logs.append(line)
        print(line, flush=True)

    def _reclaim_runtime_processes(self):
        self._reap_previous_manager_pid()
        self._cleanup_orphan_runtime_ffmpeg()
        self._prune_handoff_hls()
        self._write_manager_pid()

    def _write_manager_pid(self):
        try:
            self.managed_pidfile.parent.mkdir(parents=True, exist_ok=True)
            self.managed_pidfile.write_text(str(os.getpid()), encoding="utf-8")
        except OSError:
            pass

    def _reap_previous_manager_pid(self):
        try:
            previous = int(self.managed_pidfile.read_text(encoding="utf-8").strip() or "0")
        except (OSError, ValueError):
            previous = 0
        current = os.getpid()
        if previous and previous != current and proc_alive(previous):
            previous_cmdline = proc_cmdline(previous)
            if "app/server.py" not in previous_cmdline:
                return
            kill_pid(previous, signal.SIGTERM)
            deadline = time.time() + 5
            while time.time() < deadline and proc_alive(previous):
                time.sleep(0.1)
            if proc_alive(previous):
                kill_pid(previous, signal.SIGKILL)

    def _cleanup_orphan_runtime_ffmpeg(self):
        runtime_markers = (
            str(WORK_DIR.resolve()),
            str(HLS_DIR.resolve()),
        )
        proc_root = Path("/proc")
        for entry in proc_root.iterdir():
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            if pid == os.getpid():
                continue
            cmdline_path = entry / "cmdline"
            try:
                raw = cmdline_path.read_bytes()
            except OSError:
                continue
            if not raw:
                continue
            text = raw.replace(b"\x00", b" ").decode("utf-8", errors="ignore")
            if "ffmpeg" not in text:
                continue
            if not any(marker in text for marker in runtime_markers):
                continue
            kill_pid(pid, signal.SIGTERM)
            deadline = time.time() + 3
            while time.time() < deadline and proc_alive(pid):
                time.sleep(0.05)
            if proc_alive(pid):
                kill_pid(pid, signal.SIGKILL)

    def get_profile(self):
        with self.lock:
            return self.profile.copy()

    def get_public_profile(self):
        with self.lock:
            return _strip_url_fields(self.profile.copy())

    def normalize_profile(self, profile, save=False):
        raw_profile = profile or {}
        # Start from current saved profile to preserve existing values on partial updates
        if hasattr(self, 'profile') and self.profile:
            merged = DEFAULT_PROFILE.copy()
            merged.update(self.profile)
            merged.update(raw_profile)
        else:
            merged = DEFAULT_PROFILE.copy()
            merged.update(raw_profile)
        merged["video_url"] = str(merged.get("video_url", "") or "")
        merged["audio_url"] = str(merged.get("audio_url", "") or "")
        merged["video_headers"] = str(merged.get("video_headers", "") or "")
        merged["audio_headers"] = str(merged.get("audio_headers", "") or "")
        merged["video_playlist"] = str(merged.get("video_playlist", "") or "")
        merged["audio_playlist"] = str(merged.get("audio_playlist", "") or "")
        merged["video_local_m3u"] = str(merged.get("video_local_m3u", "") or "")
        merged["audio_local_m3u"] = str(merged.get("audio_local_m3u", "") or "")
        merged["video_primary"] = str(merged.get("video_primary", "") or "")
        merged["audio_channel"] = str(merged.get("audio_channel", "") or "")
        merged["video_fallbacks"] = [str(x).strip() for x in merged.get("video_fallbacks", []) if str(x).strip()]
        merged["audio_fallbacks"] = [str(x).strip() for x in merged.get("audio_fallbacks", []) if str(x).strip()]
        merged["offset_seconds"] = coerce_float(merged.get("offset_seconds"), DEFAULT_PROFILE["offset_seconds"])
        merged["retry_limit"] = coerce_int(merged.get("retry_limit"), DEFAULT_PROFILE["retry_limit"], minimum=1)
        merged["timeout_seconds"] = coerce_int(merged.get("timeout_seconds"), DEFAULT_PROFILE["timeout_seconds"], minimum=5)
        merged["segment_time"] = effective_segment_time(merged)
        merged["playlist_size"] = coerce_int(merged.get("playlist_size"), DEFAULT_PROFILE["playlist_size"], minimum=3)
        merged["hls_segment_type"] = hls_segment_type(merged)
        merged["local_cache_enabled"] = parse_bool(merged.get("local_cache_enabled", DEFAULT_PROFILE["local_cache_enabled"]))
        merged["local_cache_seconds"] = coerce_int(merged.get("local_cache_seconds"), DEFAULT_PROFILE["local_cache_seconds"], minimum=30)
        interval_value = raw_profile.get("auto_align_interval", raw_profile.get("snapshot_interval", merged.get("auto_align_interval")))
        merged["auto_align_interval"] = coerce_int(interval_value, DEFAULT_PROFILE["auto_align_interval"], minimum=60)
        merged["auto_align_samples"] = coerce_int(merged.get("auto_align_samples"), DEFAULT_PROFILE["auto_align_samples"], minimum=3)
        merged["auto_align_step"] = coerce_float(merged.get("auto_align_step"), DEFAULT_PROFILE["auto_align_step"], minimum=0.5)
        merged["auto_align_threshold"] = coerce_float(merged.get("auto_align_threshold"), DEFAULT_PROFILE["auto_align_threshold"], minimum=0.1)
        merged["auto_align_max_offset"] = coerce_float(merged.get("auto_align_max_offset"), DEFAULT_PROFILE["auto_align_max_offset"], minimum=1)
        merged["auto_align_relocate_attempts"] = coerce_int(merged.get("auto_align_relocate_attempts"), DEFAULT_PROFILE["auto_align_relocate_attempts"], minimum=0)
        merged["auto_align_debug_override"] = parse_bool(merged.get("auto_align_debug_override", DEFAULT_PROFILE["auto_align_debug_override"]))
        merged["snapshot_interval"] = merged["auto_align_interval"]
        merged["schedule_enabled"] = parse_bool(merged.get("schedule_enabled", DEFAULT_PROFILE["schedule_enabled"]))
        merged["schedule_provider"] = coerce_text(merged.get("schedule_provider"), DEFAULT_PROFILE["schedule_provider"])
        merged["schedule_league"] = coerce_text(merged.get("schedule_league"), DEFAULT_PROFILE["schedule_league"])
        merged["schedule_timezone"] = coerce_text(merged.get("schedule_timezone"), DEFAULT_PROFILE["schedule_timezone"])
        merged["schedule_refresh_hours"] = coerce_int(merged.get("schedule_refresh_hours"), DEFAULT_PROFILE["schedule_refresh_hours"], minimum=1)
        merged["schedule_poll_seconds"] = coerce_int(merged.get("schedule_poll_seconds"), DEFAULT_PROFILE["schedule_poll_seconds"], minimum=30)
        merged["schedule_pre_minutes"] = coerce_int(merged.get("schedule_pre_minutes"), DEFAULT_PROFILE["schedule_pre_minutes"], minimum=0)
        merged["schedule_duration_minutes"] = coerce_int(merged.get("schedule_duration_minutes"), DEFAULT_PROFILE["schedule_duration_minutes"], minimum=90)
        merged["schedule_post_minutes"] = coerce_int(merged.get("schedule_post_minutes"), DEFAULT_PROFILE["schedule_post_minutes"], minimum=0)
        merged["ocr_provider"] = normalize_ocr_provider(merged.get("ocr_provider"))
        merged["ocr_api_key"] = coerce_text(merged.get("ocr_api_key"), DEFAULT_PROFILE["ocr_api_key"])
        merged["ocrspace_api_key"] = coerce_text(merged.get("ocrspace_api_key"), DEFAULT_PROFILE.get("ocrspace_api_key", ""))
        merged["ocr_custom_endpoint"] = coerce_text(merged.get("ocr_custom_endpoint"), DEFAULT_PROFILE.get("ocr_custom_endpoint", ""))
        merged["ocr_custom_model"] = coerce_text(merged.get("ocr_custom_model"), DEFAULT_PROFILE.get("ocr_custom_model", "gpt-4o"))
        merged["auto_align_enabled"] = ocr_provider_ready(merged)
        merged.pop("auto_align_outside_match", None)
        merged.pop("align_only_during_match", None)
        merged.pop("auto_align_stop_after_aligned", None)
        merged.pop("snapshot_interval", None)
        merged.pop("video_roi", None)
        merged.pop("audio_roi", None)
        merged.pop("video_roi_presets", None)
        merged.pop("audio_roi_presets", None)
        if save:
            json_save(PROFILE_PATH, _strip_url_fields(merged))
        return merged

    def set_profile(self, profile):
        merged = self.normalize_profile(profile)
        with self.lock:
            self.profile = merged
            self.status["offset_seconds"] = merged["offset_seconds"]
            self.status["auto_align_enabled"] = ocr_provider_ready(merged)
            self.status["auto_align_msg"] = ""
            self.status["schedule"]["enabled"] = parse_bool(merged.get("schedule_enabled", DEFAULT_PROFILE.get("schedule_enabled", True)))
        json_save(PROFILE_PATH, _strip_url_fields(merged))
        json_save(
            OFFSET_STATE,
            {
                "offset_seconds": round(merged["offset_seconds"], 3),
                "updated_at_unix": int(time.time()),
                "source": "web-ui-profile",
            },
        )
        self.log("profile saved")
        return merged

    def get_status(self):
        with self.lock:
            status = self.status.copy()
            status["profile"] = _strip_url_fields(self.profile.copy())
            aa = self.profile.copy()
            status["auto_align"] = {
                "enabled": ocr_provider_ready(aa),
                "active_allowed": self._auto_align_allowed_by_schedule(aa),
                "debug_override": parse_bool(aa.get("auto_align_debug_override", DEFAULT_PROFILE["auto_align_debug_override"])),
                "interval": coerce_int(aa.get("auto_align_interval"), DEFAULT_PROFILE["auto_align_interval"]),
                "samples": coerce_int(aa.get("auto_align_samples"), DEFAULT_PROFILE["auto_align_samples"]),
                "step": coerce_float(aa.get("auto_align_step"), DEFAULT_PROFILE["auto_align_step"]),
                "threshold": coerce_float(aa.get("auto_align_threshold"), DEFAULT_PROFILE["auto_align_threshold"]),
                "max_offset": coerce_float(aa.get("auto_align_max_offset"), DEFAULT_PROFILE["auto_align_max_offset"]),
                "relocate_attempts": coerce_int(aa.get("auto_align_relocate_attempts"), DEFAULT_PROFILE["auto_align_relocate_attempts"], minimum=0),
                "snapshot_interval": coerce_int(aa.get("auto_align_interval"), DEFAULT_PROFILE["auto_align_interval"]),
            }
            status["auto_align_state"] = self.status.get("auto_align_state", "idle")
            status["auto_align_monitor"] = dict(self.status.get("auto_align_monitor") or {})
            status["hls_url"] = "/index.m3u8"
            status["emby_url"] = "/emby.m3u"
            playlist = self._current_served_hls_playlist()
            segment_names = self._playlist_segments(playlist)
            segments = [
                HLS_DIR / name
                for name in segment_names
                if name and (HLS_DIR / name).exists()
            ]
            playlist_exists = playlist.exists()
            playlist_mtime = None
            if playlist_exists:
                try:
                    playlist_mtime = playlist.stat().st_mtime
                except FileNotFoundError:
                    playlist_exists = False
            status["hls"] = {
                "playlist_exists": playlist_exists,
                "playlist_mtime": playlist_mtime,
                "segment_count": len(segments),
                "latest_segment": segments[-1].name if segments else "",
                "segment_type": hls_segment_type(self.profile),
                "source_playlist": Path(self.active_hls_playlist).name if self.active_hls_playlist else "index.m3u8",
            }
            status["schedule"] = self._schedule_status_snapshot()
            status["recording"] = self._recording_status_snapshot()
            return status

    def _current_served_hls_playlist(self):
        playlist = Path(self.active_hls_playlist or (HLS_DIR / "index.m3u8"))
        return playlist if playlist.is_absolute() else HLS_DIR / playlist

    def start(self, source="manual"):
        with self.lock:
            if self.thread and self.thread.is_alive():
                if source == "manual":
                    self.schedule_owned_run = False
                    self.manual_override_until_event_id = ""
                return
            if source == "manual":
                self.schedule_owned_run = False
                self.manual_override_until_event_id = ""
            elif source == "schedule":
                self.schedule_owned_run = True
            self.stop_event.clear()
            self.status.update({
                "running": True,
                "stage": "starting",
                "started_at": now(),
                "failure_count": 0,
                "last_error": "",
                "ocr_request_count": 0,
                "ocr_request_last_at": None,
                "ocr_request_last_provider": "",
                "first_alignment_at": None,
                "first_alignment_ocr_request_count": None,
            })
            self.thread = threading.Thread(target=self._run_loop, daemon=True)
            self.thread.start()
        self.log(f"live pipeline requested ({source})")

    def stop(self, source="manual", cancel_restart=True):
        with self.lock:
            if cancel_restart:
                self.restart_request_id += 1
        with self.lock:
            if source == "manual":
                active_id = ""
                schedule = self.status.get("schedule") or {}
                active = schedule.get("active_match") or {}
                if active:
                    active_id = active.get("event_id", "")
                self.manual_override_until_event_id = active_id or self.schedule_last_active_id
                self.schedule_owned_run = False
            elif source == "schedule":
                self.schedule_owned_run = False
        self.stop_event.set()
        self._stop_processes()
        with self.lock:
            self.status["running"] = False
            self.status["stage"] = "stopped"
            self.status["auto_align_state"] = "idle"
            self.status["auto_align_monitor"] = {}
            self.current_snapshot_jobs = []
        self.log(f"live pipeline stopped ({source})")

    def restart(self, profile=None, source="manual"):
        if profile:
            self.set_profile(profile)
        with self.lock:
            self.restart_request_id += 1
            request_id = self.restart_request_id
        self.stop(source=source, cancel_restart=False)
        with self.lock:
            thread = self.thread
            if self.thread and self.thread.is_alive():
                self.status["stage"] = "restarting"
                self.log(f"restart queued ({source}): waiting for previous pipeline to stop")
                threading.Thread(
                    target=self._restart_when_stopped,
                    args=(request_id, source),
                    name="live-sync-restart-waiter",
                    daemon=True,
                ).start()
                return
            if self.thread and not self.thread.is_alive():
                self.thread = None
        self.start(source=source)

    def _restart_when_stopped(self, request_id, source):
        while True:
            with self.lock:
                if request_id != self.restart_request_id:
                    return
                thread = self.thread
            if not thread or not thread.is_alive():
                break
            thread.join(timeout=0.5)
        with self.lock:
            if request_id != self.restart_request_id:
                return
            if self.thread and not self.thread.is_alive():
                self.thread = None
        self.start(source=source)

    def _resolve_audio_channel(self, profile, audio_sources, force=False):
        channels = selected_audio_channels(profile)
        if not channels:
            raise RuntimeError("audio channel name is empty (audio playlist is set but audio_channel field is blank)")

        last_exc = None
        for channel_name in channels:
            try:
                audio = self.resolver.find_any_sources(audio_sources, channel_name, force=force)
                return audio, channel_name
            except Exception as exc:
                last_exc = exc
                self.log(f"audio resolve failed for {channel_name}: {exc}")

        if last_exc:
            raise RuntimeError(str(last_exc))
        raise RuntimeError("audio source unavailable")

    def _run_loop(self):
        profile = self.get_profile()
        channels = [profile.get("video_primary", "")] + list(profile.get("video_fallbacks", []))
        channels = [ch for ch in channels if ch]
        video_sources = m3u_sources(profile.get("video_playlist", ""), profile.get("video_local_m3u", ""), "本地视频 M3U")
        audio_sources = m3u_sources(profile.get("audio_playlist", ""), profile.get("audio_local_m3u", ""), "本地音频 M3U")
        if not channels:
            self._fail("no video channels configured")
            return
        if not video_sources:
            self._fail("no video M3U configured")
            return

        index = 0
        failures = 0
        current_url = ""
        preserve_hls = self._should_preserve_hls()
        while not self.stop_event.is_set():
            channel_name = channels[index]
            force_refresh = failures >= int(profile.get("retry_limit", 3))
            channel_video_sources = prefer_garyshare_4k_sources(video_sources, channel_name)
            try:
                video = self.resolver.find_any_sources(channel_video_sources, channel_name, force=force_refresh)
            except Exception as exc:
                self.log(f"video resolve failed for {channel_name}: {exc}")
                index += 1
                failures = 0
                if index >= len(channels):
                    index = 0
                    self.log("all selected video channels failed to resolve; retrying from the first configured channel")
                    time.sleep(5)
                continue

            try:
                if not audio_sources:
                    audio = Channel(name="no audio", url="")
                    active_audio_channel = ""
                    self.log("no audio M3U configured; running video-only")
                else:
                    audio, active_audio_channel = self._resolve_audio_channel(profile, audio_sources, force=force_refresh)
            except Exception as exc:
                self.log(f"audio resolve failed: {exc}")
                self._fail(f"audio source unavailable: {exc}")
                return

            if force_refresh and current_url and video.url != current_url:
                self.log(f"{channel_name} URL changed after refresh; switching to updated URL")
                failures = 0
            elif force_refresh:
                index += 1
                failures = 0
                if index >= len(channels):
                    index = 0
                    self.log("all selected video channels failed; retrying from the first configured channel")
                    time.sleep(5)
                    continue
                self.log(f"{channel_name} URL unchanged after {profile.get('retry_limit', 3)} failures; falling back to {channels[index]}")
                continue

            current_url = video.url
            with self.lock:
                self.status.update({
                    "stage": "running",
                    "active_channel": channel_name,
                    "active_url": _redact_url(video.url),
                    "active_audio_channel": active_audio_channel,
                    "audio_url": _redact_url(audio.url),
                    "failure_count": failures,
                    "last_resolution": f"{channel_name} -> {_redact_url(video.url)}",
                    "offset_seconds": profile.get("offset_seconds", 0),
                })

            reason = self._run_pipeline(video, audio, profile, preserve_hls=preserve_hls)
            preserve_hls = self._should_preserve_hls()
            if self.stop_event.is_set():
                break
            failure = reason if isinstance(reason, PipelineFailure) else PipelineFailure(str(reason))
            if failure.reason.startswith("re-aligned"):
                failures = 0
                continue
            if failure.kind == "audio":
                failures = 0
            else:
                failures += 1
            with self.lock:
                self.status["failure_count"] = failures
                self.status["last_error"] = failure.reason
            if failure.kind == "audio":
                self.log(f"pipeline failed for {channel_name}: {failure.reason} (audio source; keeping video channel)")
            else:
                self.log(f"pipeline failed for {channel_name}: {failure.reason} (failure {failures}/{profile.get('retry_limit', 3)})")

        self._stop_processes()
        with self.lock:
            self.status["running"] = False
            self.status["stage"] = "stopped"

    def _fail(self, message):
        self.log(message)
        with self.lock:
            self.status.update({"running": False, "stage": "stopped", "last_error": message, "auto_align_state": "idle", "auto_align_monitor": {}})

    def _clear_hls(self):
        HLS_DIR.mkdir(parents=True, exist_ok=True)
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        self.active_hls_playlist = HLS_DIR / "index.m3u8"
        self.hls_preserved_assets.clear()
        for item in HLS_DIR.iterdir():
            if item.name == "snapshots":
                continue
            if item.is_dir():
                shutil.rmtree(item, ignore_errors=True)
            else:
                item.unlink(missing_ok=True)

    def _start_process(self, cmd, name, context=""):
        self.log(f"starting {name}: {' '.join(_redact_url(part) for part in cmd)}")
        if context:
            self.log(f"{name} context: {context}")
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, bufsize=1, preexec_fn=os.setsid)
        with self.lock:
            self.processes.append(proc)
            self.process_tails[proc.pid] = deque(maxlen=20)
        threading.Thread(target=self._read_stderr, args=(proc, name, context), daemon=True).start()
        return proc

    def _read_stderr(self, proc, name, context=""):
        if not proc.stderr:
            return
        for line in proc.stderr:
            line = line.strip()
            if line:
                with self.lock:
                    tail = self.process_tails.get(proc.pid)
                    if tail is not None:
                        tail.append(line)
                self.log(f"{name}: {_redact_url(line)}")
                context_patterns = (
                    "expired from playlists",
                    "HTTP error 403",
                    "Failed to open segment",
                    "failed too many times",
                    "Server returned 403",
                    "Invalid argument",
                    "error opening input",
                )
                if context and any(pattern.lower() in line.lower() for pattern in context_patterns):
                    self.log(f"{name}: upstream context: {context}")
                if "failed too many times" in line and proc.poll() is None and env_bool("TERMINATE_ON_SEGMENT_FAILURE", False):
                    self.log(f"{name}: terminating ffmpeg after repeated upstream segment failures")
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                    except ProcessLookupError:
                        pass

    def _process_tail_summary(self, proc, max_lines=6):
        lines = self._process_tail_lines(proc, max_lines=max_lines)
        if not lines:
            return ""
        return " | recent stderr: " + " || ".join(_redact_url(line) for line in lines)

    def _process_tail_lines(self, proc, max_lines=6):
        if proc is None:
            return []
        with self.lock:
            lines = list(self.process_tails.get(proc.pid, []))[-max_lines:]
        return lines

    def _mux_failure_detail(self, mux, pipeline):
        context = f" | context: {pipeline.video_input_label}; {pipeline.audio_input_label}"
        tail = self._process_tail_summary(mux, max_lines=12)
        return context + tail

    def _source_cache_failure(self, source_cache):
        if not source_cache:
            return None
        checks = (
            ("video", source_cache.video_proc),
            ("audio", source_cache.audio_proc),
        )
        for kind, proc in checks:
            if not proc or proc.poll() is None:
                continue
            tail = self._process_tail_summary(proc, max_lines=8)
            return PipelineFailure(f"{kind} source cache exited with code {proc.returncode}{tail}", kind=kind)
        return None

    def _playlist_latest_mtime(self, playlist_path):
        playlist = Path(playlist_path or "")
        paths = [playlist]
        for segment in self._playlist_segments(playlist):
            paths.append(playlist.parent / segment)
        mtimes = []
        for path in paths:
            try:
                if path.exists():
                    mtimes.append(path.stat().st_mtime)
            except FileNotFoundError:
                pass
        return max(mtimes) if mtimes else 0

    def _source_cache_stall_failure(self, source_cache, timeout):
        if not source_cache:
            return None
        now_ts = time.time()
        checks = []
        if source_cache.video and source_cache.video.url:
            checks.append(("video", source_cache.video.url))
        if source_cache.audio and source_cache.audio.url:
            checks.append(("audio", source_cache.audio.url))
        stale = []
        fresh = []
        for kind, playlist in checks:
            mtime = self._playlist_latest_mtime(playlist)
            if not mtime:
                stale.append((kind, "missing"))
            elif now_ts - mtime > timeout:
                stale.append((kind, f"{now_ts - mtime:.1f}s old"))
            else:
                fresh.append(kind)
        if len(stale) == 1:
            kind, detail = stale[0]
            return PipelineFailure(f"{kind} source cache stopped updating ({detail})", kind=kind)
        if stale and not fresh:
            details = ", ".join(f"{kind} {detail}" for kind, detail in stale)
            return PipelineFailure(f"source caches stopped updating ({details})", kind="unknown")
        return None

    def _classify_stall_failure(self, source_cache, detail):
        cache_failure = self._source_cache_failure(source_cache)
        if cache_failure:
            return cache_failure
        text = str(detail or "").lower()
        audio_markers = (
            "input1=local cache audio",
            "source=audio",
            "audio_cache",
            "audio-cache",
            "audio source cache",
        )
        video_markers = (
            "input0=local video",
            "source=video",
            "video_cache",
            "video-cache",
            "video source cache",
        )
        has_audio = any(marker in text for marker in audio_markers)
        has_video = any(marker in text for marker in video_markers)
        if has_audio and not has_video:
            return PipelineFailure(detail, kind="audio")
        return PipelineFailure(detail, kind="unknown")

    def _stop_processes(self, procs=None):
        if procs is None:
            procs = list(self.processes)
            self.processes.clear()
        else:
            unique = []
            seen = set()
            for proc in procs:
                if proc is None or id(proc) in seen:
                    continue
                seen.add(id(proc))
                unique.append(proc)
            procs = unique
            targets = {id(proc) for proc in procs}
            self.processes = [proc for proc in self.processes if id(proc) not in targets]
        for proc in procs:
            if proc.poll() is None:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except ProcessLookupError:
                    pass
        deadline = time.time() + 5
        for proc in procs:
            while proc.poll() is None and time.time() < deadline:
                time.sleep(0.1)
            if proc.poll() is None:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
        with self.lock:
            for proc in procs:
                self.process_tails.pop(proc.pid, None)

    def _hls_segment_number(self, path):
        match = re.match(r"live_(\d+)\.(?:ts|m4s)$", path.name)
        return int(match.group(1)) if match else None

    def _max_hls_segment_number(self):
        numbers = [self._hls_segment_number(p) for p in list(HLS_DIR.glob("live_*.ts")) + list(HLS_DIR.glob("live_*.m4s"))]
        numbers = [num for num in numbers if num is not None]
        return max(numbers) if numbers else -1

    def _next_hls_start_number(self):
        return self._max_hls_segment_number() + 1

    def _wait_for_playlist(self, playlist, proc, timeout, label):
        deadline = time.time() + timeout
        while time.time() < deadline and not self.stop_event.is_set():
            if playlist.exists():
                return
            if proc.poll() is not None:
                raise RuntimeError(f"{label} exited with code {proc.returncode}")
            time.sleep(0.2)
        if self.stop_event.is_set():
            raise RuntimeError("stopped")
        raise RuntimeError(f"{label} playlist was not created")

    def _http_input_options(self, url, headers=None):
        if not str(url or "").lower().startswith(("http://", "https://")):
            return []
        headers = {**default_request_headers(), **dict(headers or {})}
        user_agent = headers.pop("User-Agent", "") or ffmpeg_user_agent()
        args = [
            "-user_agent", user_agent,
            "-protocol_whitelist", "file,http,https,tcp,tls,crypto,data,pipe",
            "-reconnect", "1",
            "-reconnect_on_network_error", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "10",
        ]
        if urlsplit(str(url)).path.lower().endswith(".m3u8"):
            args += ["-http_persistent", "0", "-live_start_index", "-1"]
        if headers:
            header_text = "".join(f"{key}: {value}\r\n" for key, value in headers.items())
            args += ["-headers", header_text]
        return args

    def _local_hls_input_options(self, url, live_start_index=None):
        if live_start_index is None:
            return []
        text = str(url or "").lower()
        if text.startswith(("http://", "https://")) or not text.endswith(".m3u8"):
            return []
        return ["-live_start_index", str(live_start_index)]

    def _probe_video_codec(self, channel, timeout):
        if not channel or not channel.url:
            return ""
        cmd = [
            "ffprobe", "-hide_banner", "-loglevel", "error",
            "-rw_timeout", str(timeout * 1_000_000),
            *self._http_input_options(channel.url, channel.headers),
            *self._local_hls_input_options(channel.url, -1),
            "-select_streams", "v:0",
            "-show_entries", "stream=codec_name",
            "-of", "default=nokey=1:noprint_wrappers=1",
            channel.url,
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 5, check=True)
        except Exception as exc:
            self.log(f"video codec probe failed for {channel.name}: {exc}; using generic copy path")
            return ""
        codec = result.stdout.strip().splitlines()
        return codec[0].strip().lower() if codec else ""

    def _probe_audio_streams(self, channel, timeout):
        if not channel or not channel.url:
            return []
        cmd = [
            "ffprobe", "-hide_banner", "-loglevel", "error",
            "-rw_timeout", str(timeout * 1_000_000),
            *self._http_input_options(channel.url, channel.headers),
            *self._local_hls_input_options(channel.url, -1),
            "-select_streams", "a",
            "-show_entries", "stream=index,codec_name",
            "-of", "json",
            channel.url,
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 5, check=True)
        except Exception as exc:
            self.log(f"audio stream probe failed for {channel.name}: {exc}; using first audio stream")
            return []
        try:
            data = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            return []
        streams = []
        for relative_idx, stream in enumerate(data.get("streams") or []):
            codec = str(stream.get("codec_name") or "").strip().lower()
            streams.append({
                "relative_index": relative_idx,
                "absolute_index": stream.get("index"),
                "codec": codec,
            })
        return streams

    def _select_audio_stream(self, channel, timeout):
        streams = self._probe_audio_streams(channel, timeout)
        selected = None
        for stream in streams:
            if stream.get("codec") == "aac":
                selected = stream
                break
        if selected is None and streams:
            selected = streams[0]
        if not selected:
            return 0, ""
        relative_idx = int(selected.get("relative_index") or 0)
        codec = selected.get("codec") or ""
        if codec == "aac":
            self.log(f"audio stream selected for {channel.name}: a:{relative_idx} codec=aac")
        elif codec:
            self.log(f"audio stream selected for {channel.name}: a:{relative_idx} codec={codec}; AAC not found")
        return relative_idx, codec

    def _video_copy_args(self, codec):
        args = ["-c:v", "copy"]
        if codec in ("hevc", "h265"):
            args += ["-tag:v", "hvc1"]
            if strip_dovi_rpu():
                args += ["-bsf:v", "filter_units=remove_types=62"]
        return args

    def _input_is_hls(self, input_args):
        if not input_args:
            return False
        for idx, arg in enumerate(input_args):
            if arg == "-i" and idx + 1 < len(input_args):
                source = str(input_args[idx + 1]).strip().lower()
                return source.endswith(".m3u8")
        return False

    def _start_local_cache_recorder(self, channel, playlist, profile, kind, audio_stream_index=0):
        segment = f"{effective_segment_time(profile):.3f}"
        list_size = local_cache_list_size(profile)
        segment_name = playlist.parent / f"{kind}_cache_%06d.ts"
        # Keep a video stream in the audio cache so OCR/snapshot jobs can read
        # the commentary timer frame while mux still maps only the audio stream.
        map_args = ["-map", "0:v:0?", "-map", f"0:a:{int(audio_stream_index)}?"]
        header_label = f", headers={','.join(channel.headers.keys())}" if channel.headers else ""
        context = (
            f"source={kind} url={channel.url}{header_label}; "
            f"cache={playlist.name}; seconds={effective_local_cache_seconds(profile)}; copy"
        )
        return self._start_process([
            "ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
            "-rw_timeout", str(coerce_int(profile.get("timeout_seconds"), DEFAULT_PROFILE["timeout_seconds"], minimum=5) * 1_000_000),
            *self._http_input_options(channel.url, channel.headers),
            "-fflags", "+discardcorrupt",
            "-thread_queue_size", "4096", "-i", channel.url,
            *map_args,
            "-c", "copy",
            "-f", "hls", "-hls_time", segment, "-hls_list_size", str(list_size),
            "-hls_delete_threshold", "2",
            "-hls_flags", "delete_segments+omit_endlist+append_list",
            "-hls_segment_filename", str(segment_name),
            str(playlist),
        ], f"{kind}-cache", context=context)

    def _start_local_source_cache(self, video, audio, profile):
        cache_dir = WORK_DIR / "source_cache"
        wait_timeout = coerce_int(profile.get("timeout_seconds"), DEFAULT_PROFILE["timeout_seconds"], minimum=5) + int(effective_segment_time(profile)) + 5
        last_exc = None
        for attempt in range(2):
            shutil.rmtree(cache_dir, ignore_errors=True)
            cache_dir.mkdir(parents=True, exist_ok=True)
            video_playlist = cache_dir / "video_cache.m3u8"
            audio_playlist = cache_dir / "audio_cache.m3u8"
            audio_proc = None
            video_proc = None
            try:
                same_source = bool(audio and audio.url) and video.url == audio.url and dict(video.headers) == dict(audio.headers)
                source_audio = video if same_source else audio
                audio_stream_index, _audio_codec = self._select_audio_stream(source_audio, coerce_int(profile.get("timeout_seconds"), DEFAULT_PROFILE["timeout_seconds"], minimum=5)) if source_audio and source_audio.url else (0, "")
                video_proc = self._start_local_cache_recorder(video, video_playlist, profile, "video", audio_stream_index=audio_stream_index if same_source else 0)
                if audio and audio.url:
                    if same_source:
                        cached_audio = Channel(name=audio.name, url=str(video_playlist), tvg_id=audio.tvg_id, tvg_name=audio.tvg_name, group=audio.group)
                    else:
                        audio_proc = self._start_local_cache_recorder(audio, audio_playlist, profile, "audio", audio_stream_index=audio_stream_index)
                        cached_audio = Channel(name=audio.name, url=str(audio_playlist), tvg_id=audio.tvg_id, tvg_name=audio.tvg_name, group=audio.group)
                else:
                    cached_audio = Channel(name="no audio", url="")
                cached_video = Channel(name=video.name, url=str(video_playlist), tvg_id=video.tvg_id, tvg_name=video.tvg_name, group=video.group)
                waits = [(video_playlist, video_proc, "video cache")]
                if audio_proc:
                    waits.append((audio_playlist, audio_proc, "audio cache"))
                for playlist, proc, label in waits:
                    self._wait_for_playlist(playlist, proc, wait_timeout, label)
                self.log("local cache ready: upstream requests are held by cache recorders; mux/snapshot/OCR read local HLS")
                return LocalSourceCache(cache_dir, cached_video, cached_audio, video_proc, audio_proc)
            except Exception as exc:
                last_exc = exc
                self._stop_processes([proc for proc in (video_proc, audio_proc) if proc])
                shutil.rmtree(cache_dir, ignore_errors=True)
                if attempt == 0:
                    self.log(f"local cache warmup failed once: {exc}; retrying")
                    time.sleep(2)
                    continue
                raise
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("local cache warmup failed")

    def _start_delay_recorder(self, channel, playlist, segment, list_size, timeout, kind, video_codec="", audio_stream_index=0):
        url = channel.url
        segment_name = playlist.parent / f"{kind}_%06d.ts"
        if kind == "video":
            stream_args = ["-map", "0:v:0", "-an", *self._video_copy_args(video_codec)]
        else:
            # Keep an optional video stream in delayed audio HLS so snapshot/OCR
            # can still read frames from the commentary source during re-alignment.
            stream_args = ["-map", "0:v:0?", "-map", f"0:a:{int(audio_stream_index)}?", "-c", "copy"]
        header_label = f", headers={','.join(channel.headers.keys())}" if channel.headers else ""
        context = f"source={kind} url={url}{header_label}; output={playlist.name}"
        return self._start_process([
            "ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
            "-rw_timeout", str(timeout * 1_000_000),
            *self._http_input_options(url, channel.headers),
            *self._local_hls_input_options(url, -1),
            "-fflags", "+discardcorrupt",
            "-thread_queue_size", "4096", "-i", url,
            *stream_args,
            "-f", "hls", "-hls_time", segment, "-hls_list_size", str(list_size),
            "-hls_flags", "delete_segments+omit_endlist+append_list",
            "-hls_segment_filename", str(segment_name),
            str(playlist),
        ], f"{kind}-delay", context=context)

    def _buffer_delay_input(self, recorder, playlist, offset, timeout, stage):
        started = time.time()
        self._wait_for_playlist(playlist, recorder, timeout, stage)
        remaining = max(0, abs(offset) - (time.time() - started))
        with self.lock:
            self.status["stage"] = f"{stage} {abs(offset):.1f}s"
        while remaining > 0 and not self.stop_event.is_set() and recorder.poll() is None:
            sleep_for = min(1, remaining)
            time.sleep(sleep_for)
            remaining -= sleep_for
        if self.stop_event.is_set():
            raise RuntimeError("stopped")
        if recorder.poll() is not None:
            raise RuntimeError(f"{stage} exited with code {recorder.returncode}")
    def _buffer_delay_input(self, recorder, playlist, offset, timeout, stage):
        started = time.time()
        self._wait_for_playlist(playlist, recorder, timeout, stage)
        remaining = max(0, abs(offset) - (time.time() - started))
        with self.lock:
            self.status["stage"] = f"{stage} {abs(offset):.1f}s"
        while remaining > 0 and not self.stop_event.is_set() and recorder.poll() is None:
            sleep_for = min(1, remaining)
            time.sleep(sleep_for)
            remaining -= sleep_for
        if self.stop_event.is_set():
            raise RuntimeError("stopped")
        if recorder.poll() is not None:
            raise RuntimeError(f"{stage} exited with code {recorder.returncode}")
        # Ensure at least one segment file exists before letting mux consume this playlist
        deadline = time.time() + max(45, timeout * 2)
        while time.time() < deadline and not self.stop_event.is_set():
            segments = self._playlist_segments(playlist)
            ready = [seg for seg in segments if (playlist.parent / seg).exists()]
            if ready:
                return
            if recorder.poll() is not None:
                raise RuntimeError(f"{stage} exited with code {recorder.returncode} (no segments)")
            time.sleep(0.5)
        if self.stop_event.is_set():
            raise RuntimeError("stopped")
        raise RuntimeError(f"{stage} produced no segments within timeout")

    def _direct_input(self, url, timeout, headers=None, live_start_index=None):
        if live_start_index is None:
            text = str(url or "").lower()
            if text.endswith(".m3u8") and not text.startswith(("http://", "https://")):
                live_start_index = -1
        return [
            "-rw_timeout", str(timeout * 1_000_000),
            *self._http_input_options(url, headers),
            *self._local_hls_input_options(url, live_start_index),
            "-fflags", "+discardcorrupt",
            "-thread_queue_size", "4096", "-i", url,
        ]

    def _prepare_pipeline(self, video, audio, profile, run_label, source_cache=None):
        video_url = video.url
        audio_url = audio.url if audio else ""
        run_dir = WORK_DIR / run_label
        shutil.rmtree(run_dir, ignore_errors=True)
        run_dir.mkdir(parents=True, exist_ok=True)

        offset = float(profile.get("offset_seconds") or 0)
        timeout = coerce_int(profile.get("timeout_seconds"), DEFAULT_PROFILE["timeout_seconds"], minimum=5)
        segment_seconds = effective_segment_time(profile)
        segment = f"{segment_seconds:.3f}"
        delay_procs = []
        video_codec = self._probe_video_codec(video, timeout)
        audio_stream_index, audio_codec = self._select_audio_stream(audio, timeout) if audio_url else (0, "")
        channel_text = " ".join([
            str(getattr(video, "name", "") or ""),
            str(getattr(video, "tvg_name", "") or ""),
            str(getattr(video, "tvg_id", "") or ""),
            str(profile.get("channel_name", "") or ""),
            str(profile.get("video_primary", "") or ""),
        ]).lower()
        channel_prefers_fmp4 = "4k" in channel_text
        configured_segment_type = hls_segment_type(profile)
        compatible_mux = configured_segment_type == "auto" and not channel_prefers_fmp4
        mux_segment_type = configured_segment_type if configured_segment_type != "auto" else ("fmp4" if channel_prefers_fmp4 else "mpegts")
        if video_codec:
            mux_mode = "auto mpegts" if compatible_mux else mux_segment_type
            self.log(f"video codec detected: {video_codec}; mux mode: {mux_mode}")
        same_source = (
            bool(audio_url)
            and video_url == audio_url
            and dict(video.headers) == dict(audio.headers)
        )
        single_input_av = same_source and not source_cache and abs(offset) < 0.5
        cache_live_start = -1 if source_cache else None
        video_input = self._direct_input(video_url, timeout, video.headers, live_start_index=cache_live_start)
        video_header_label = f", headers={','.join(video.headers.keys())}" if video.headers else ""
        audio_header_label = f", headers={','.join(audio.headers.keys())}" if audio_url and audio.headers else ""
        source_kind = "local cache" if source_cache else "direct"
        video_input_label = f"input0={source_kind} video ({video_url}{video_header_label})"
        audio_input = []
        audio_map = ""
        if single_input_av:
            audio_map = f"0:a:{audio_stream_index}"
            audio_input_label = f"input0=audio from same {source_kind} source ({audio_url}{audio_header_label})"
        elif audio_url:
            audio_input = self._direct_input(audio_url, timeout, audio.headers, live_start_index=cache_live_start)
            audio_map = f"1:a:{audio_stream_index}"
            audio_input_label = f"input1={source_kind} audio ({audio_url}{audio_header_label})"
        else:
            audio_input_label = "input1=none"
        video_snapshot_input = list(video_input)
        audio_snapshot_input = list(video_input if single_input_av else audio_input)
        snapshot_jobs = []
        audio_copy_bsf = ""

        try:
            if offset >= 0.5:
                delay_playlist = run_dir / "video_delay.m3u8"
                list_size = max(20, int(offset / segment_seconds) + 20)
                recorder = self._start_delay_recorder(video, delay_playlist, segment, list_size, timeout, "video", video_codec)
                delay_procs.append(recorder)
                self._buffer_delay_input(recorder, delay_playlist, offset, timeout, "buffering video")
                # HLS live_start_index is from the end when negative; -1 means latest segment.
                video_input = ["-thread_queue_size", "4096", "-live_start_index", "-1", "-i", str(delay_playlist)]
                video_input_label = f"input0=local video delay HLS ({delay_playlist.name}, source={video_url}{video_header_label}, offset +{offset:.3f}s)"
                video_snapshot_input = list(video_input)
            elif offset <= -0.5:
                if not audio_url:
                    raise RuntimeError("negative offset requires an audio source")
                delay_playlist = run_dir / "audio_delay.m3u8"
                list_size = max(20, int(abs(offset) / segment_seconds) + 20)
                recorder = self._start_delay_recorder(audio, delay_playlist, segment, list_size, timeout, "audio", audio_stream_index=audio_stream_index)
                delay_procs.append(recorder)
                self._buffer_delay_input(recorder, delay_playlist, offset, timeout, "buffering audio")
                audio_input = ["-thread_queue_size", "4096", "-live_start_index", "-1", "-i", str(delay_playlist)]
                audio_map = "1:a:0"
                audio_input_label = f"input1=local audio delay HLS ({delay_playlist.name}, source={audio_url}{audio_header_label}, offset {offset:.3f}s)"
                audio_snapshot_input = list(audio_input)
        except Exception:
            self._stop_processes(delay_procs)
            shutil.rmtree(run_dir, ignore_errors=True)
            raise

        if source_cache:
            snapshot_jobs.append(("cache_video", list(self._direct_input(source_cache.video.url, timeout, video.headers, live_start_index=-1)), f"{video.name} 缓存前"))
            if source_cache.audio and source_cache.audio.url:
                snapshot_jobs.append(("cache_audio", list(self._direct_input(source_cache.audio.url, timeout, audio.headers, live_start_index=-1)), f"{audio.name} 缓存前"))
        else:
            snapshot_jobs.append(("cache_video", list(self._direct_input(video_url, timeout, video.headers)), f"{video.name} 原始"))
            if audio_url:
                cache_audio_input = list(video_input if single_input_av else self._direct_input(audio_url, timeout, audio.headers))
                snapshot_jobs.append(("cache_audio", cache_audio_input, f"{audio.name} 原始"))
        snapshot_jobs.append(("video", list(video_snapshot_input), f"{video.name} 延迟后"))
        if audio_snapshot_input:
            snapshot_jobs.append(("audio", list(audio_snapshot_input), f"{audio.name if audio else 'audio'} 延迟后"))

        audio_copy_bsf = ""
        if audio_map and output_audio_codec() == "copy":
            source_input = video_input if single_input_av else audio_input
            if audio_codec == "aac" and mux_segment_type == "fmp4" and self._input_is_hls(source_input):
                audio_copy_bsf = "aac_adtstoasc"

        return PreparedPipeline(
            offset=offset,
            run_dir=run_dir,
            video_input=video_input,
            audio_input=audio_input,
            delay_procs=delay_procs,
            audio_map=audio_map,
            audio_copy_bsf=audio_copy_bsf,
            single_input_av=single_input_av,
            video_codec=video_codec,
            compatible_mux=compatible_mux,
            channel_prefers_fmp4=channel_prefers_fmp4,
            video_input_label=video_input_label,
            audio_input_label=audio_input_label,
            video_snapshot_input=video_snapshot_input,
            audio_snapshot_input=audio_snapshot_input,
            snapshot_jobs=snapshot_jobs,
        )

    def _reserve_handoff_start_number(self, profile):
        playlist_size = coerce_int(profile.get("playlist_size"), DEFAULT_PROFILE["playlist_size"], minimum=3)
        return self._next_hls_start_number() + max(1000, playlist_size * 4)

    def _start_mux(self, prepared, profile, *, append=False, start_number=None, playlist_path=None, discontinuity=False):
        segment = f"{effective_segment_time(profile):.3f}"
        playlist_size = coerce_int(profile.get("playlist_size"), DEFAULT_PROFILE["playlist_size"], minimum=3)
        segment_ext = effective_hls_segment_ext(profile, prepared)
        segment_type = effective_hls_segment_type(profile, prepared)
        if start_number is None:
            start_number = self._next_hls_start_number() if append else 0
        playlist_path = Path(playlist_path or (HLS_DIR / "index.m3u8"))
        hls_flags = "delete_segments+omit_endlist"
        if append:
            hls_flags += "+append_list"
        if discontinuity:
            hls_flags += "+discont_start"

        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
            *prepared.video_input,
        ]
        if prepared.audio_input:
            cmd += prepared.audio_input
        cmd += ["-map", "0:v:0"]
        if prepared.audio_map:
            cmd += ["-map", prepared.audio_map]
        cmd += self._video_copy_args(prepared.video_codec)
        if prepared.audio_map:
            cmd += ["-c:a", "copy"]
            if prepared.audio_copy_bsf:
                cmd += ["-bsf:a", prepared.audio_copy_bsf]
        cmd += [
            "-f", "hls", "-hls_time", segment, "-hls_list_size", str(playlist_size),
            "-hls_delete_threshold", "2",
            "-start_number", str(start_number),
            "-hls_flags", hls_flags,
            "-hls_segment_type", segment_type,
        ]
        if segment_type == "fmp4":
            cmd += ["-hls_fmp4_init_filename", f"init_{playlist_path.stem}.mp4"]
        cmd += [
            "-hls_segment_filename", str(HLS_DIR / f"live_%06d{segment_ext}"),
            str(playlist_path),
        ]
        context = f"{prepared.video_input_label}; {prepared.audio_input_label}; output={playlist_path.name}"
        return self._start_process(cmd, "mux", context=context)

    def _playlist_segments(self, playlist_path):
        try:
            lines = playlist_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except FileNotFoundError:
            return []
        return [
            Path(line.strip()).name
            for line in lines
            if line.strip() and not line.startswith("#") and (".ts" in line or ".m4s" in line)
        ]

    def _wait_for_handoff_segment(self, playlist_path, mux, timeout, minimum_segments=1):
        deadline = time.time() + timeout
        while time.time() < deadline and not self.stop_event.is_set():
            if mux.poll() is not None:
                raise RuntimeError(f"ffmpeg exited with code {mux.returncode}{self._process_tail_summary(mux)}")
            segments = self._playlist_segments(playlist_path)
            ready = [seg for seg in segments if (HLS_DIR / seg).exists()]
            if len(ready) >= minimum_segments:
                return
            time.sleep(0.25)
        if self.stop_event.is_set():
            raise RuntimeError("stopped")
        raise RuntimeError(f"no HLS segment created after handoff within {timeout}s")

    def _save_auto_offset(self, offset):
        with self.lock:
            self.profile["offset_seconds"] = offset
            self.status["offset_seconds"] = offset
            if self.status.get("first_alignment_at") is None:
                self.status["first_alignment_at"] = now()
                self.status["first_alignment_ocr_request_count"] = int(self.status.get("ocr_request_count") or 0)
            profile = self.profile.copy()
        json_save(PROFILE_PATH, _strip_url_fields(profile))
        json_save(OFFSET_STATE, {
            "offset_seconds": round(offset, 3),
            "updated_at_unix": int(time.time()),
            "source": "auto-align",
        })

    def _handoff_pipeline(self, video, audio, profile, old_pipeline, old_mux, run_label, source_cache=None):
        timeout = coerce_int(profile.get("timeout_seconds"), DEFAULT_PROFILE["timeout_seconds"], minimum=5)
        warmup_timeout = max(timeout, 45)
        warmup_segments = min(3, coerce_int(profile.get("playlist_size"), DEFAULT_PROFILE["playlist_size"], minimum=3))
        new_mux = None
        staging_playlist = HLS_DIR / f"{run_label}.m3u8"
        staging_playlist.unlink(missing_ok=True)
        with self.lock:
            self.status["stage"] = f"preparing handoff {float(profile.get('offset_seconds') or 0):.1f}s"
        self.log(f"handoff: preparing offset {float(profile.get('offset_seconds') or 0):.3f}s")
        try:
            prepared = self._prepare_pipeline(video, audio, profile, run_label, source_cache=source_cache)
        except Exception as exc:
            raise HandoffDeferred(f"prepare failed: {exc}") from exc
        try:
            start_number = self._reserve_handoff_start_number(profile)
            self.log(f"handoff: warming new HLS writer at segment {start_number:06d}")
            new_mux = self._start_mux(
                prepared, profile, start_number=start_number,
                playlist_path=staging_playlist, discontinuity=True
            )
            self._wait_for_handoff_segment(staging_playlist, new_mux, warmup_timeout, warmup_segments)
        except Exception as exc:
            cleanup = [new_mux, *prepared.delay_procs] if new_mux else prepared.delay_procs
            self._stop_processes(cleanup)
            shutil.rmtree(prepared.run_dir, ignore_errors=True)
            staging_playlist.unlink(missing_ok=True)
            raise HandoffDeferred(f"warmup failed: {exc}") from exc

        self.log(f"handoff: publishing warmed playlist {staging_playlist.name}")
        self._stop_processes([old_mux, *old_pipeline.delay_procs])
        shutil.rmtree(old_pipeline.run_dir, ignore_errors=True)
        self._publish_hls_playlist(staging_playlist)
        with self.lock:
            self.status["stage"] = "running"
        self.log(f"handoff: appended new stream at offset {prepared.offset:.3f}s")
        return prepared, new_mux

    def _publish_hls_playlist(self, playlist_path):
        previous = self._current_served_hls_playlist()
        if previous.exists() and previous != Path(playlist_path):
            self._remember_hls_assets(previous)
        self.active_hls_playlist = Path(playlist_path)
        self._sync_active_hls_playlist(force=True)
        self._prune_handoff_hls()

    def _sync_active_hls_playlist(self, force=False):
        active = Path(self.active_hls_playlist or (HLS_DIR / "index.m3u8"))
        index = HLS_DIR / "index.m3u8"
        if active == index:
            return
        if not active.exists():
            raise FileNotFoundError(f"active HLS playlist missing: {active.name}")
        try:
            source_text = active.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise RuntimeError(f"failed to read active HLS playlist {active.name}: {exc}") from exc
        if not force and index.exists():
            try:
                if index.read_text(encoding="utf-8", errors="replace") == source_text:
                    return
            except OSError:
                pass
        tmp = HLS_DIR / ".index.m3u8.tmp"
        tmp.write_text(source_text, encoding="utf-8")
        os.replace(tmp, index)

    def _playlist_referenced_files(self, playlist_path):
        playlist = Path(playlist_path)
        referenced = set(self._playlist_segments(playlist))
        try:
            for line in playlist.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                map_match = HLS_MAP_URI_RE.search(line)
                if map_match:
                    referenced.add(Path(map_match.group(1)).name)
                elif not line.startswith("#") and line.endswith(".mp4"):
                    referenced.add(Path(line).name)
        except FileNotFoundError:
            return set()
        return referenced

    def _trim_preserved_hls_references(self, referenced):
        referenced = set(referenced or ())
        if not referenced:
            return referenced
        playlist_size = coerce_int(self.profile.get("playlist_size"), DEFAULT_PROFILE["playlist_size"], minimum=3)
        keep_live = max(playlist_size, 12) + 4
        live_segments = []
        for name in referenced:
            path = Path(name)
            if path.name.startswith("live_") and path.suffix in (".ts", ".m4s"):
                number = self._hls_segment_number(path)
                if number is not None:
                    live_segments.append((number, path.name))
        live_segments.sort()
        keep_live_names = {name for _number, name in live_segments[-keep_live:]}
        trimmed = set()
        for name in referenced:
            path = Path(name)
            if path.name.startswith("live_") and path.suffix in (".ts", ".m4s"):
                if path.name in keep_live_names:
                    trimmed.add(path.name)
                continue
            trimmed.add(path.name)
        return trimmed

    def _remember_hls_assets(self, playlist_path):
        referenced = self._trim_preserved_hls_references(self._playlist_referenced_files(playlist_path))
        if not referenced:
            return
        self.hls_preserved_assets.append({
            "playlist": Path(playlist_path).name,
            "expires_at": time.time() + HLS_CLIENT_GRACE_SECONDS,
            "referenced": referenced,
        })

    def _preserve_current_index_assets(self):
        index = HLS_DIR / "index.m3u8"
        if not index.exists():
            return
        self._remember_hls_assets(index)

    def _preserved_hls_assets(self):
        now_ts = time.time()
        active_entries = deque(maxlen=self.hls_preserved_assets.maxlen)
        referenced = set()
        for entry in self.hls_preserved_assets:
            if float(entry.get("expires_at") or 0) <= now_ts:
                continue
            active_entries.append(entry)
            referenced.update(set(entry.get("referenced") or ()))
        self.hls_preserved_assets = active_entries
        return referenced

    def _prune_handoff_hls(self):
        if not HLS_DIR.exists():
            return
        playlist = HLS_DIR / "index.m3u8"
        active_playlist = Path(self.active_hls_playlist or playlist)
        referenced = self._preserved_hls_assets()
        for current in {playlist, active_playlist}:
            try:
                referenced.update(self._playlist_referenced_files(current))
            except FileNotFoundError:
                continue

        segments = sorted(
            list(HLS_DIR.glob("live_*.ts")) + list(HLS_DIR.glob("live_*.m4s")),
            key=lambda p: self._hls_segment_number(p) if self._hls_segment_number(p) is not None else -1,
        )
        keep_recent = {p.name for p in segments[-max(12, len(referenced) + 4):]}
        keep = referenced | keep_recent | {"index.m3u8", active_playlist.name}
        for item in HLS_DIR.iterdir():
            if item.name in keep or item.name == "snapshots":
                continue
            if item.name.startswith("live_") and item.suffix in (".ts", ".m4s"):
                item.unlink(missing_ok=True)
            elif item.name.startswith("init_") and item.suffix == ".mp4":
                item.unlink(missing_ok=True)
            elif re.match(r"^run_\d+\.m3u8$", item.name):
                item.unlink(missing_ok=True)
        self._prune_work_runs()

    def _prune_work_runs(self):
        if not WORK_DIR.exists():
            return
        active_run_dir = None
        with self.lock:
            jobs = list(self.current_snapshot_jobs)
        for _kind, input_args, _source in jobs:
            for idx, arg in enumerate(input_args):
                if arg == "-i" and idx + 1 < len(input_args):
                    source = str(input_args[idx + 1])
                    if source.startswith(str(WORK_DIR)) and "run_" in source:
                        active_run_dir = Path(source).resolve().parent
                        break
            if active_run_dir is not None:
                break

        cutoff = time.time() - HLS_CLIENT_GRACE_SECONDS
        for run_dir in WORK_DIR.glob("run_*"):
            if not run_dir.is_dir():
                continue
            if active_run_dir and run_dir.resolve() == active_run_dir:
                continue
            try:
                mtime = run_dir.stat().st_mtime
            except FileNotFoundError:
                continue
            if mtime > cutoff:
                continue
            shutil.rmtree(run_dir, ignore_errors=True)

    def _set_align_monitor_status(self, monitor, msg=None):
        if msg is not None:
            monitor.message = msg
        with self.lock:
            self.status["auto_align_state"] = monitor.state
            self.status["auto_align_msg"] = monitor.message
            self.status["auto_align_monitor"] = monitor.snapshot()

    def _set_current_snapshot_jobs(self, pipeline, profile):
        jobs = list(pipeline.snapshot_jobs or [])
        with self.lock:
            self.current_snapshot_jobs = jobs

    def _extract_current_frame(self, url, out_path, timeout, headers=None):
        subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-rw_timeout", str(timeout * 1_000_000),
            *self._http_input_options(url, headers),
            *self._local_hls_input_options(url, -1),
            "-i", url,
            "-frames:v", "1", "-update", "1", str(out_path),
        ], capture_output=True, timeout=timeout + 5, check=True)

    def _capture_frame_at_deadline(self, kind, url, out_path, timeout, headers, barrier, deadline_ns):
        barrier.wait()
        now_ns = time.monotonic_ns()
        if deadline_ns and now_ns < deadline_ns:
            time.sleep((deadline_ns - now_ns) / 1_000_000_000)
        started_at = time.monotonic()
        self._extract_current_frame(url, out_path, timeout, headers)
        finished_at = time.monotonic()
        return FrameCaptureResult(Path(out_path), kind, started_at, finished_at, source=kind)

    def _capture_frame_pair(self, video, audio, tmpdir, timeout, *, deadline_delay=1.25):
        video_frame = tmpdir / "align_video.jpg"
        audio_frame = tmpdir / "align_audio.jpg"
        barrier = threading.Barrier(2)
        deadline_ns = time.monotonic_ns() + int(max(0.0, deadline_delay) * 1_000_000_000)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futs = [
                pool.submit(self._capture_frame_at_deadline, "video", video.url, video_frame, timeout, video.headers, barrier, deadline_ns),
                pool.submit(self._capture_frame_at_deadline, "audio", audio.url, audio_frame, timeout, audio.headers, barrier, deadline_ns),
            ]
            results = []
            for fut in as_completed(futs):
                results.append(fut.result())
        results.sort(key=lambda item: item.kind)
        return results[0], results[1]

    def _capture_alignment_frames(self, video, audio, tmpdir, timeout):
        video_cap, audio_cap = self._capture_frame_pair(video, audio, tmpdir, timeout)
        return video_cap.path, audio_cap.path

    def _pair_capture_skew(self, left, right):
        return abs(left.started_at - right.started_at)

    def _read_locked_clock(self, frame_path, roi, kind=None):
        if roi is None:
            return None
        parsed = self._ocr_time(frame_path, roi)
        if not parsed:
            if kind:
                with self.lock:
                    self.status.setdefault("last_ocr_results", {})[kind] = {"clock": None, "error": "OCR failed"}
            return None
        result = ClockSample(0.0, parsed[0], parsed[1], roi)
        if kind:
            with self.lock:
                self.status.setdefault("last_ocr_results", {})[kind] = {
                    "clock": result.text,
                    "game_time": result.game_time,
                    "updated_at": time.strftime("%H:%M:%S", time.localtime()),
                }
        return result

    def _read_top_quarter_clock(self, frame_path, kind=None):
        return self._read_locked_clock(frame_path, self._scoreboard_top_quarter_roi(), kind=kind)

    def _roi_store(self):
        data = self.roi if isinstance(self.roi, dict) else {}
        if "channels" not in data or not isinstance(data.get("channels"), dict):
            data = {
                "version": 2,
                "channels": {},
                "legacy": data,
            }
            self.roi = data
        data.setdefault("version", 2)
        data.setdefault("channels", {})
        return data

    def _save_roi_store(self):
        with self.lock:
            data = self._roi_store()
            snapshot = json.loads(json.dumps(data))
        json_save(ROI_PATH, snapshot)

    def _channel_roi_key(self, kind, channel):
        channel = channel or Channel(name=kind, url="")
        basis = "|".join([
            kind,
            channel.tvg_id or "",
            channel.tvg_name or "",
            channel.name or "",
            channel.group or "",
        ]).strip("|")
        readable = normalize_key_text(channel.tvg_id or channel.tvg_name or channel.name or kind) or kind
        digest_basis = basis or f"{kind}|{channel.url or ''}"
        digest = hashlib.sha1(digest_basis.encode("utf-8", errors="ignore")).hexdigest()[:12]
        return f"{kind}:{readable}:{digest}"

    def _channel_roi_entry(self, kind, channel):
        key = self._channel_roi_key(kind, channel)
        with self.lock:
            entry = dict(self._roi_store().get("channels", {}).get(key) or {})
        roi = entry.get("roi")
        try:
            parsed_roi = parse_roi(",".join(str(part) for part in roi)) if isinstance(roi, (list, tuple)) else parse_roi(roi)
        except (TypeError, ValueError):
            return key, None
        entry["roi"] = parsed_roi
        return key, entry

    def _timer_preview_roi(self, roi):
        x, y, w, h = roi
        cx = x + w / 2
        cy = y + h / 2
        pw = min(1.0, w * TIMER_ROI_PREVIEW_MULTIPLIER)
        ph = min(1.0, h * TIMER_ROI_PREVIEW_MULTIPLIER)
        px = min(max(0.0, cx - pw / 2), max(0.0, 1.0 - pw))
        py = min(max(0.0, cy - ph / 2), max(0.0, 1.0 - ph))
        return px, py, pw, ph

    def _scoreboard_top_half_roi(self):
        return (0.0, 0.0, 1.0, 0.5)

    def _scoreboard_top_quarter_roi(self):
        return (0.0, 0.0, 1.0, 0.25)

    def _scoreboard_top_quadrant_roi(self, side):
        side = str(side or "").strip().lower()
        if side in ("right", "2"):
            return (0.5, 0.0, 0.5, 0.5)
        if side in ("left", "1"):
            return (0.0, 0.0, 0.5, 0.5)
        return None

    def _is_scoreboard_top_scan_roi(self, roi):
        if not roi:
            return False
        return tuple(round(float(part), 3) for part in roi) in (
            (0.0, 0.0, 1.0, 0.5),
            (0.0, 0.0, 0.5, 0.5),
            (0.5, 0.0, 0.5, 0.5),
        )

    def _scoreboard_scan_roi(self, side="left", width=0.5, height=0.5):
        width = min(max(width, 0.5), 1.0)
        height = min(max(height, 0.25), 1.0)
        if side == "right":
            x = 1.0 - width
        elif side == "center":
            x = max(0.0, (1.0 - width) / 2)
        else:
            x = 0.0
        return x, 0.0, width, height

    def _save_timer_roi_preview(self, frame_path, kind, key, roi):
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        ROI_PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
        crop = self._roi_crop(frame_path, self._timer_preview_roi(roi))
        if crop is None:
            return ""
        safe_key = re.sub(r"[^A-Za-z0-9_.-]+", "_", key)[:96]
        out = ROI_PREVIEW_DIR / f"{safe_key}.jpg"
        tmp = ROI_PREVIEW_DIR / f".{safe_key}.tmp.jpg"
        cv2.imwrite(str(tmp), crop, [int(cv2.IMWRITE_JPEG_QUALITY), 95])
        os.replace(tmp, out)
        return out.name

    def _save_channel_roi(self, kind, channel, sample, source, frame_path=None, key=None):
        if not sample or not sample.roi:
            return None
        if key is None:
            key = self._channel_roi_key(kind, channel)
        preview = ""
        if frame_path is not None:
            preview = self._save_timer_roi_preview(frame_path, kind, key, sample.roi)
        entry = {
            "key": key,
            "kind": kind,
            "channel": channel.name if channel else kind,
            "tvg_id": channel.tvg_id if channel else "",
            "tvg_name": channel.tvg_name if channel else "",
            "group": channel.group if channel else "",
            "roi": [round(float(part), 6) for part in sample.roi],
            "source": source,
            "clock": sample.text,
            "preview": preview,
            "updated_at": now(),
            "updated_at_unix": int(time.time()),
        }
        with self.lock:
            store = self._roi_store()
            old = store["channels"].get(key) or {}
            if not preview and old.get("preview"):
                entry["preview"] = old.get("preview", "")
            store["channels"][key] = entry
            snapshot = json.loads(json.dumps(store))
        json_save(ROI_PATH, snapshot)
        return entry

    def _timer_roi_entries(self):
        with self.lock:
            channels = dict(self._roi_store().get("channels", {}))
        entries = []
        for key, raw in channels.items():
            if not isinstance(raw, dict):
                continue
            entry = dict(raw)
            entry["key"] = key
            preview = str(entry.get("preview") or "")
            if preview:
                path = ROI_PREVIEW_DIR / preview
                entry["preview_url"] = f"/roi-previews/{preview}" if path.exists() else ""
                try:
                    entry["preview_mtime"] = path.stat().st_mtime if path.exists() else 0
                except FileNotFoundError:
                    entry["preview_mtime"] = 0
            else:
                entry["preview_url"] = ""
                entry["preview_mtime"] = 0
            entries.append(entry)
        entries.sort(key=lambda item: (item.get("kind") != "video", item.get("channel", ""), item.get("key", "")))
        return entries

    def delete_timer_roi(self, key, *, roi=True, preview=True):
        key = str(key or "").strip()
        if not key:
            raise RuntimeError("missing ROI key")
        removed = {}
        with self.lock:
            store = self._roi_store()
            entry = store["channels"].get(key)
            if not entry:
                return {"ok": True, "key": key, "removed": False}
            removed = dict(entry)
            if preview and entry.get("preview"):
                removed["preview"] = entry.get("preview")
                entry["preview"] = ""
            if roi:
                removed = dict(store["channels"].pop(key))
            snapshot = json.loads(json.dumps(store))
        if preview and removed.get("preview"):
            (ROI_PREVIEW_DIR / removed["preview"]).unlink(missing_ok=True)
        json_save(ROI_PATH, snapshot)
        self.log(f"timer ROI {'deleted' if roi else 'preview deleted'}: {key}")
        return {"ok": True, "key": key, "removed": True}

    def _preset_rois_for_kind(self, profile, kind):
        key = "audio_roi_presets" if kind == "audio" else "video_roi_presets"
        rois = parse_roi_list(profile.get(key, ""))
        bad_lines = len(split_lines(profile.get(key, ""))) - len(rois)
        if bad_lines > 0:
            self.log(f"auto-align: ignored {bad_lines} invalid {kind} ROI preset(s)")
        return rois

    def _read_preset_clock(self, frame_path, profile, kind):
        for roi in self._preset_rois_for_kind(profile, kind):
            sample = self._read_locked_clock(frame_path, roi)
            if sample:
                return sample
        return None

    def _find_frame_clock(self, frame_path, *, full_frame=False, profile=None):
        if profile is None:
            profile = self.get_profile()
        providers = ocr_provider_order(profile)
        if not providers:
            return None
        for idx, provider in enumerate(providers):
            if idx > 0 and not ocr_provider_ready_for(provider, profile):
                continue
            if idx > 0 and full_frame:
                self.log(f"OCR primary '{providers[0]}' failed, fallback to '{provider}'")
            found = coerce_clock_sample(self._try_ocr_provider(provider, frame_path, profile))
            if found:
                return found
        return None

    def _try_ocr_provider(self, provider, frame_path, profile):
        if provider == "ocrspace":
            return self._find_clock_via_ocrspace(frame_path, profile)
        elif provider == "custom":
            return self._find_clock_via_custom(frame_path, profile)
        return None

    def _find_clock_with_presets(self, frame_path, profile, kind):
        sample = self._read_preset_clock(frame_path, profile, kind)
        if sample:
            return sample, "preset"
        sample = self._find_frame_clock(frame_path, profile=profile)
        if sample:
            return sample, "auto"
        return None, ""

    def _resolve_monitor_roi_for_frame(self, frame_path, kind, profile, monitor, channel_name):
        ch_key = monitor.video_channel_key if kind == "video" else monitor.audio_channel_key
        # 1. Channel cache ROI
        if ch_key:
            with self.lock:
                entry = dict(self._roi_store().get("channels", {}).get(ch_key) or {})
            if entry and entry.get("roi"):
                try:
                    raw = entry["roi"]
                    if isinstance(raw, (list, tuple)):
                        roi = parse_roi(",".join(str(p) for p in raw))
                    else:
                        roi = parse_roi(str(raw))
                    if self._clock_candidate_roi_plausible(roi):
                        sample = self._read_locked_clock(frame_path, roi)
                        if sample:
                            return sample, "cache"
                except (TypeError, ValueError):
                    pass
        # 2. Preset ROIs (first-run fallback)
        sample = self._read_preset_clock(frame_path, profile, kind)
        if sample:
            chan = Channel(name=channel_name, url="")
            self._save_channel_roi(kind, chan, sample, "preset", frame_path=frame_path, key=ch_key)
            return sample, "preset"
        # 3. Profile manual ROI (legacy override)
        roi_key = "audio_roi" if kind == "audio" else "video_roi"
        try:
            manual_roi = parse_roi(profile.get(roi_key, DEFAULT_PROFILE[roi_key]))
            if self._clock_candidate_roi_plausible(manual_roi):
                sample = self._read_locked_clock(frame_path, manual_roi)
                if sample:
                    chan = Channel(name=channel_name, url="")
                    self._save_channel_roi(kind, chan, sample, "manual", frame_path=frame_path, key=ch_key)
                    return sample, "manual"
        except (TypeError, ValueError):
            pass
        now = time.time()
        probe_at = monitor.video_next_probe_at if kind == "video" else monitor.audio_next_probe_at
        search_started = monitor.video_search_started_at if kind == "video" else monitor.audio_search_started_at
        if probe_at > 0 and now < probe_at:
            return None, ""
        if search_started > 0 and now - search_started > TIMER_ROI_SCAN_WINDOW_SECONDS:
            return None, ""
        if search_started <= 0:
            if kind == "video":
                monitor.video_search_started_at = now
            else:
                monitor.audio_search_started_at = now
        sample = self._find_frame_clock(frame_path, full_frame=True)
        if kind == "video":
            monitor.video_next_probe_at = now + TIMER_ROI_SCAN_INTERVAL_SECONDS
        else:
            monitor.audio_next_probe_at = now + TIMER_ROI_SCAN_INTERVAL_SECONDS
        if sample:
            chan = Channel(name=channel_name, url="")
            self._save_channel_roi(kind, chan, sample, "scan", frame_path=frame_path, key=ch_key)
            return sample, "scan"
        return None, ""

    def _lock_monitor_roi(self, monitor, kind, sample, profile, source):
        if kind == "audio":
            monitor.audio_roi = sample.roi
            monitor.audio_roi_locked = True
            monitor.audio_clock = sample.text
        else:
            monitor.video_roi = sample.roi
            monitor.video_roi_locked = True
            monitor.video_clock = sample.text
        self._save_monitor_rois(profile, monitor)
        self.log(f"auto-align: locked {kind} ROI from {source} {self._format_roi_value(sample.roi)} ({sample.text})")

    def _candidate_offset_from_samples(self, video_sample, audio_sample, profile):
        offset = -(audio_sample.game_time - video_sample.game_time)
        max_offset = coerce_float(profile.get("auto_align_max_offset"), DEFAULT_PROFILE["auto_align_max_offset"], minimum=1)
        if abs(offset) > max_offset:
            return None, f"offset {offset:.3f}s exceeds +/-{max_offset}s"
        return offset, f"v={video_sample.text} a={audio_sample.text}"

    def _sample_pair_offset(self, video_sample, audio_sample, profile):
        offset = -(audio_sample.game_time - video_sample.game_time)
        max_offset = coerce_float(profile.get("auto_align_max_offset"), DEFAULT_PROFILE["auto_align_max_offset"], minimum=1)
        if abs(offset) > max_offset:
            return None
        return offset

    def _offset_cluster_center(self, offsets):
        if not offsets:
            return None
        ordered = sorted(offsets)
        median = ordered[len(ordered) // 2]
        deviations = [abs(item - median) for item in ordered]
        mad = sorted(deviations)[len(deviations) // 2]
        limit = max(1.5, mad * 3.0)
        cluster = [item for item in ordered if abs(item - median) <= limit]
        if len(cluster) < 3:
            return None
        cluster.sort()
        return cluster[len(cluster) // 2]

    def _stable_mismatch_offset(self, offsets):
        if not offsets:
            return None
        return self._offset_cluster_center(offsets)

    def _monitor_alignment_from_frames(self, video_frame, audio_frame, profile, monitor):
        threshold = coerce_float(profile.get("auto_align_threshold"), DEFAULT_PROFILE["auto_align_threshold"], minimum=0.1)
        current = float(profile.get("offset_seconds", 0) or 0)
        monitor.checks += 1
        video_sample = self._read_top_quarter_clock(video_frame, kind="video")
        audio_sample = self._read_top_quarter_clock(audio_frame, kind="audio")

        if not video_sample or not audio_sample:
            if not video_sample:
                monitor.video_missing_count += 1
            else:
                monitor.video_missing_count = 0
            if not audio_sample:
                monitor.audio_missing_count += 1
            else:
                monitor.audio_missing_count = 0
            total_missing = max(monitor.video_missing_count, monitor.audio_missing_count)
            monitor.state = "acquiring"
            missing = []
            if not video_sample:
                missing.append("video")
            if not audio_sample:
                missing.append("audio")
            monitor.message = f"timer missing ({total_missing}/3): {', '.join(missing)}"
            return None, monitor.message

        monitor.video_missing_count = 0
        monitor.audio_missing_count = 0
        monitor.video_clock = video_sample.text
        monitor.audio_clock = audio_sample.text

        candidate = self._sample_pair_offset(video_sample, audio_sample, profile)
        if candidate is None:
            monitor.state = "acquiring"
            monitor.message = "offset exceeds max bound"
            return None, monitor.message

        delta = abs(candidate - current)
        if video_sample.game_time == audio_sample.game_time and delta >= threshold:
            monitor.state = "realigning"
            monitor.message = f"matched clocks; new offset {candidate:.3f}s v={video_sample.text} a={audio_sample.text}"
            self.log(f"auto-align: {monitor.message}")
            return candidate, monitor.message

        if (
            monitor.checks <= 2
            and delta >= max(threshold * 3, 3.0)
            and abs(video_sample.game_time - audio_sample.game_time) >= max(threshold * 3, 3.0)
        ):
            monitor.state = "realigning"
            monitor.message = f"early mismatch; new offset {candidate:.3f}s v={video_sample.text} a={audio_sample.text}"
            self.log(f"auto-align: {monitor.message}")
            return candidate, monitor.message

        if delta < threshold:
            monitor.state = "aligned"
            monitor.message = f"stable current={current:.3f}s candidate={candidate:.3f}s v={video_sample.text} a={audio_sample.text}"
            monitor.mismatch_offsets.clear()
            monitor.mismatch_count = 0
            return None, monitor.message

        monitor.mismatch_offsets = [candidate]
        monitor.mismatch_count = 1
        monitor.state = "realigning"
        monitor.message = f"mismatch; new offset {candidate:.3f}s v={video_sample.text} a={audio_sample.text}"
        self.log(f"auto-align: {monitor.message}")
        return candidate, monitor.message

    def _monitor_alignment_once(self, video, audio, profile, monitor):
        if not audio or not audio.url:
            monitor.state = "disabled"
            monitor.message = "video-only; no audio clock to compare"
            return None, monitor.message

        timeout = coerce_int(profile.get("timeout_seconds"), DEFAULT_PROFILE["timeout_seconds"], minimum=5)
        with tempfile.TemporaryDirectory(prefix="align_frame_") as tmp:
            tmpdir = Path(tmp)
            try:
                video_cap, audio_cap = self._capture_frame_pair(video, audio, tmpdir, timeout)
            except Exception as exc:
                monitor.state = "capture_failed"
                monitor.message = f"frame capture failed: {exc}"
                self.log(f"auto-align: {monitor.message}; keeping current offset")
                return None, monitor.message
            if self._pair_capture_skew(video_cap, audio_cap) > 0.75 or abs(video_cap.finished_at - audio_cap.finished_at) > 1.0:
                monitor.state = "capture_failed"
                monitor.message = "paired capture skew too large; retrying"
                return None, monitor.message
            return self._monitor_alignment_from_frames(video_cap.path, audio_cap.path, profile, monitor)

    def _read_verify_clock(self, frame_path, kind, profile, monitor):
        return self._read_top_quarter_clock(frame_path)

    def _verify_alignment_candidate(self, video, audio, profile, monitor, candidate):
        if not audio or not audio.url:
            return False, "video-only; no audio clock to compare"
        threshold = coerce_float(profile.get("auto_align_threshold"), DEFAULT_PROFILE["auto_align_threshold"], minimum=0.1)
        timeout = coerce_int(profile.get("timeout_seconds"), DEFAULT_PROFILE["timeout_seconds"], minimum=5)
        segment_seconds = effective_segment_time(profile)
        verify_video = video
        verify_audio = audio
        verify_proc = None
        verify_dir = None
        previous_stage = ""
        with self.lock:
            previous_stage = self.status.get("stage", "")
        try:
            if candidate >= 0.5:
                verify_dir = Path(tempfile.mkdtemp(prefix="align_verify_"))
                verify_playlist = verify_dir / "video_delay.m3u8"
                list_size = max(20, int(candidate / segment_seconds) + 20)
                video_codec = self._probe_video_codec(video, timeout)
                verify_proc = self._start_delay_recorder(video, verify_playlist, f"{segment_seconds:.3f}", list_size, timeout, "video", video_codec)
                try:
                    self._buffer_delay_input(verify_proc, verify_playlist, candidate, timeout, "verifying video")
                finally:
                    with self.lock:
                        if self.status.get("stage", "").startswith("verifying "):
                            self.status["stage"] = previous_stage or "running"
                verify_video = Channel(name=video.name, url=str(verify_playlist))
            elif candidate <= -0.5:
                verify_dir = Path(tempfile.mkdtemp(prefix="align_verify_"))
                verify_playlist = verify_dir / "audio_delay.m3u8"
                list_size = max(20, int(abs(candidate) / segment_seconds) + 20)
                audio_stream_index, _audio_codec = self._select_audio_stream(audio, timeout)
                verify_proc = self._start_delay_recorder(audio, verify_playlist, f"{segment_seconds:.3f}", list_size, timeout, "audio", audio_stream_index=audio_stream_index)
                try:
                    self._buffer_delay_input(verify_proc, verify_playlist, candidate, timeout, "verifying audio")
                finally:
                    with self.lock:
                        if self.status.get("stage", "").startswith("verifying "):
                            self.status["stage"] = previous_stage or "running"
                verify_audio = Channel(name=audio.name, url=str(verify_playlist))

            with tempfile.TemporaryDirectory(prefix="align_verify_frame_") as tmp:
                tmpdir = Path(tmp)
                try:
                    video_cap, audio_cap = self._capture_frame_pair(verify_video, verify_audio, tmpdir, timeout)
                except Exception as exc:
                    return False, f"verify capture failed: {exc}"
                if self._pair_capture_skew(video_cap, audio_cap) > 0.75 or abs(video_cap.finished_at - audio_cap.finished_at) > 1.0:
                    return False, "verify capture skew too large"
                video_sample = self._read_verify_clock(video_cap.path, "video", profile, monitor)
                audio_sample = self._read_verify_clock(audio_cap.path, "audio", profile, monitor)
                if not video_sample or not audio_sample:
                    return False, "verify OCR failed"
                delta = abs(video_sample.game_time - audio_sample.game_time)
                if delta <= threshold:
                    return True, f"verified offset {candidate:.3f}s v={video_sample.text} a={audio_sample.text}"
                return False, f"verify mismatch {delta:.1f}s v={video_sample.text} a={audio_sample.text}"
        finally:
            self._stop_processes([verify_proc] if verify_proc else [])
            if verify_dir:
                shutil.rmtree(verify_dir, ignore_errors=True)
            with self.lock:
                if self.status.get("stage", "").startswith("verifying "):
                    self.status["stage"] = previous_stage or "running"

    def _refresh_runtime_auto_align_profile(self, profile):
        with self.lock:
            latest = self.profile.copy()
        for key in RUNTIME_AUTO_ALIGN_KEYS:
            if key in latest:
                profile[key] = latest[key]
        return profile

    def _run_pipeline(self, video, audio, profile, preserve_hls=False):
        video_url = video.url
        audio_url = audio.url if audio else ""
        self._stop_processes()
        preserve_hls = preserve_hls or self._should_preserve_hls()
        if preserve_hls:
            self._preserve_current_index_assets()
        if not preserve_hls:
            self._clear_hls()
        WORK_DIR.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(WORK_DIR, ignore_errors=True)
        WORK_DIR.mkdir(parents=True, exist_ok=True)

        timeout = coerce_int(profile.get("timeout_seconds"), DEFAULT_PROFILE["timeout_seconds"], minimum=5)
        stall_timeout = hls_stall_timeout(profile)
        startup_timeout = startup_hls_wait_timeout(profile)
        segment_seconds = effective_segment_time(profile)
        source_cache = None
        pipeline_video = video
        pipeline_audio = audio
        if local_cache_enabled(profile):
            try:
                source_cache = self._start_local_source_cache(video, audio, profile)
                pipeline_video = source_cache.video
                pipeline_audio = source_cache.audio
            except Exception as exc:
                text = str(exc).lower()
                kind = "audio" if "audio cache" in text else "video" if "video cache" in text else "unknown"
                return PipelineFailure(f"local source cache failed: {exc}", kind=kind)
        run_id = 0
        try:
            current_pipeline = self._prepare_pipeline(pipeline_video, pipeline_audio, profile, f"run_{run_id:03d}", source_cache=source_cache)
        except Exception as exc:
            return str(exc)
        self._set_current_snapshot_jobs(current_pipeline, profile)
        playlist_path = HLS_DIR / "index.m3u8"
        start_number = self._next_hls_start_number() if preserve_hls else 0
        self.active_hls_playlist = playlist_path
        if preserve_hls and (HLS_DIR / "index.m3u8").exists():
            start_number = self._reserve_handoff_start_number(profile)
            playlist_path = HLS_DIR / f"run_{start_number:06d}.m3u8"
            playlist_path.unlink(missing_ok=True)
        mux = self._start_mux(current_pipeline, profile, start_number=start_number, playlist_path=playlist_path, discontinuity=preserve_hls)
        if playlist_path.name != "index.m3u8":
            try:
                self._wait_for_handoff_segment(
                    playlist_path,
                    mux,
                    max(stall_timeout, 45),
                    min(3, coerce_int(profile.get("playlist_size"), DEFAULT_PROFILE["playlist_size"], minimum=3)),
                )
                self.log(f"handoff: publishing warmed startup playlist {playlist_path.name}")
                self._publish_hls_playlist(playlist_path)
            except Exception as exc:
                self._stop_processes([mux, *current_pipeline.delay_procs])
                shutil.rmtree(current_pipeline.run_dir, ignore_errors=True)
                playlist_path.unlink(missing_ok=True)
                return self._classify_stall_failure(source_cache, f"warmup failed: {exc}")

        first_segment_deadline = time.time() + startup_timeout
        last_snapshot_check = 0.0
        last_runtime_prune_check = 0.0
        auto_align_profile = profile.copy()
        align_monitor = AlignmentMonitor()
        align_monitor.video_channel = video.name if video else ""
        align_monitor.audio_channel = audio.name if audio and audio.url else ""
        align_monitor.video_channel_key = self._channel_roi_key("video", video)
        align_monitor.audio_channel_key = self._channel_roi_key("audio", audio) if audio and audio.url else ""
        align_monitor.state = "acquiring"
        align_monitor.message = "reading timer from top quarter"
        with self.lock:
            self.status["auto_align_state"] = align_monitor.state
            self.status["auto_align_msg"] = align_monitor.message
            self.status["auto_align_monitor"] = align_monitor.snapshot()
        while not self.stop_event.is_set():
            auto_align_profile = self._refresh_runtime_auto_align_profile(auto_align_profile)
            cache_failure = self._source_cache_failure(source_cache)
            if cache_failure:
                return cache_failure
            if Path(self.active_hls_playlist or (HLS_DIR / "index.m3u8")).name != "index.m3u8":
                try:
                    self._sync_active_hls_playlist()
                except Exception as exc:
                    return PipelineFailure(f"failed to refresh index playlist from {Path(self.active_hls_playlist).name}: {exc}")
            if time.time() - last_runtime_prune_check >= 30:
                last_runtime_prune_check = time.time()
                self._prune_handoff_hls()
            if mux.poll() is not None:
                return f"ffmpeg exited with code {mux.returncode}{self._mux_failure_detail(mux, current_pipeline)}"
            mtime = self._latest_hls_mtime()
            if mtime:
                with self.lock:
                    self.status["last_segment_at"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime))
                    self.status["stage"] = "running"
                if time.time() - mtime > stall_timeout:
                    age = time.time() - mtime
                    detail = (
                        f"no new HLS segment for {stall_timeout}s "
                        f"(last segment {age:.1f}s ago, input timeout {timeout}s, segment {segment_seconds:.3f}s)"
                        f"{self._mux_failure_detail(mux, current_pipeline)}"
                    )
                    return self._source_cache_stall_failure(source_cache, stall_timeout) or self._classify_stall_failure(source_cache, detail)
            elif time.time() > first_segment_deadline:
                detail = (
                    f"no HLS segment created within {startup_timeout}s "
                    f"(input timeout {timeout}s, segment {segment_seconds:.3f}s)"
                    f"{self._mux_failure_detail(mux, current_pipeline)}"
                )
                return self._source_cache_stall_failure(source_cache, stall_timeout) or self._classify_stall_failure(source_cache, detail)
            cycle_interval = coerce_float(auto_align_profile.get("auto_align_interval"), DEFAULT_PROFILE["auto_align_interval"], minimum=5)
            align_allowed_now = self._auto_align_allowed_by_schedule(auto_align_profile)
            if not align_allowed_now and align_monitor.state != "disabled":
                align_monitor.state = "disabled"
                self._set_align_monitor_status(align_monitor, "非比赛时间：直播继续，暂停自动截图和对齐")
            current_provider = normalize_ocr_provider(auto_align_profile.get("ocr_provider"))
            current_key_name = "ocr_api_key" if current_provider == "custom" else "ocrspace_api_key"
            api_key = coerce_text(auto_align_profile.get(current_key_name, "")).strip()
            if not api_key:
                align_monitor.state = "disabled"
                self._set_align_monitor_status(align_monitor, "OCR 未配置 API Key，暂停自动对齐")
            # Adaptive frequency: high before alignment, low after
            if align_monitor.state == "aligned":
                probe_interval = max(cycle_interval * 5, 300)
            elif align_monitor.locked():
                probe_interval = max(cycle_interval, TIMER_ROI_SCAN_INTERVAL_SECONDS)
            else:
                probe_interval = max(cycle_interval, TIMER_ROI_SCAN_INTERVAL_SECONDS)
            if align_allowed_now and time.time() - last_snapshot_check >= probe_interval and mtime:
                last_snapshot_check = time.time()
                frames = {}
                try:
                    _results, errors, frames = self._capture_runtime_snapshots_now(pipeline_video, pipeline_audio, auto_align_profile)
                    if errors:
                        self.log("auto snapshot failed: " + "; ".join(f"{kind}: {msg}" for kind, msg in errors.items()))
                    if ocr_provider_ready(auto_align_profile):
                        if not pipeline_audio or not pipeline_audio.url:
                            align_monitor.state = "disabled"
                            a_msg = "video-only; no audio clock to compare"
                            new_off = None
                        else:
                            new_off, a_msg = self._monitor_alignment_once(
                                pipeline_video, pipeline_audio, auto_align_profile, align_monitor
                            )
                        if new_off is not None:
                            align_monitor.state = "verifying"
                            self._set_align_monitor_status(align_monitor, f"{a_msg}; verifying candidate {new_off:.3f}s")
                            verified, verify_msg = self._verify_alignment_candidate(
                                pipeline_video, pipeline_audio, auto_align_profile, align_monitor, new_off
                            )
                            if not verified:
                                a_msg = f"{a_msg}; {verify_msg}; keeping current offset"
                                self.log(f"auto-align: {a_msg}")
                                self._set_align_monitor_status(align_monitor, a_msg)
                                continue
                            a_msg = f"{a_msg}; {verify_msg}"
                            next_profile = auto_align_profile.copy()
                            next_profile["offset_seconds"] = new_off
                            try:
                                run_id += 1
                                current_pipeline, mux = self._handoff_pipeline(
                                    pipeline_video, pipeline_audio, next_profile,
                                    current_pipeline, mux, f"run_{run_id:03d}", source_cache=source_cache
                                )
                                auto_align_profile = next_profile
                                self._set_current_snapshot_jobs(current_pipeline, auto_align_profile)
                                self._save_auto_offset(new_off)
                                align_monitor.mismatch_offsets.clear()
                                align_monitor.mismatch_count = 0
                                align_monitor.state = "aligned"
                                first_segment_deadline = time.time() + timeout
                                self._set_align_monitor_status(align_monitor, f"{a_msg}; handoff complete")
                            except Exception as exc:
                                msg = f"handoff failed: {exc}; keeping current stream"
                                self.log(msg)
                                with self.lock:
                                    self.status["auto_align_msg"] = msg
                                    self.status["stage"] = "running"
                                if not isinstance(exc, HandoffDeferred):
                                    return f"handoff failed: {exc}"
                        else:
                            self._set_align_monitor_status(align_monitor, a_msg)
                finally:
                    self._cleanup_snapshot_frames(frames)
            time.sleep(2)
        return "stopped"

    def _latest_hls_mtime(self):
        files = list(HLS_DIR.glob("live_*.ts")) + list(HLS_DIR.glob("live_*.m4s")) + list(HLS_DIR.glob("init_*.mp4")) + [HLS_DIR / "index.m3u8"]
        mtimes = []
        for p in files:
            if not p.exists():
                continue
            try:
                mtimes.append(p.stat().st_mtime)
            except FileNotFoundError:
                pass
        return max(mtimes) if mtimes else 0

    def _should_preserve_hls(self):
        latest = self._latest_hls_mtime()
        if not latest:
            return False
        return time.time() - latest <= HLS_RECOVERY_WINDOW_SECONDS

    # ── Match schedule automation ───────────────────────────────────

    def _schedule_tz(self, profile):
        try:
            return ZoneInfo(coerce_text(profile.get("schedule_timezone"), DEFAULT_PROFILE["schedule_timezone"]))
        except Exception:
            return ZoneInfo(DEFAULT_PROFILE["schedule_timezone"])

    def _utc_date_range(self, days_back=1, days_forward=3):
        today = datetime.now(timezone.utc).date()
        return [(today + timedelta(days=delta)).strftime("%Y%m%d") for delta in range(-days_back, days_forward + 1)]

    def _schedule_url(self, profile, date_value):
        league = coerce_text(profile.get("schedule_league"), DEFAULT_PROFILE["schedule_league"])
        base = f"https://site.api.espn.com/apis/site/v2/sports/soccer/{league}/scoreboard"
        parts = urlsplit(base)
        query = dict(parse_qsl(parts.query))
        query["dates"] = date_value
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))

    def _parse_match_datetime(self, value):
        if not value:
            return None
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    def _event_name(self, event):
        name = event.get("name") or event.get("shortName") or event.get("id") or "Match"
        short_name = event.get("shortName") or name
        competitors = (event.get("competitions") or [{}])[0].get("competitors") or []
        labels = []
        for item in competitors:
            team = item.get("team") or {}
            labels.append(team.get("shortDisplayName") or team.get("displayName") or team.get("abbreviation") or "")
        labels = [label for label in labels if label]
        if len(labels) >= 2:
            short_name = " vs ".join(labels[:2])
            name = short_name
        return name, short_name

    def _parse_schedule_events(self, payload, profile):
        pre = coerce_int(profile.get("schedule_pre_minutes"), DEFAULT_PROFILE["schedule_pre_minutes"], minimum=0) * 60
        duration = coerce_int(profile.get("schedule_duration_minutes"), DEFAULT_PROFILE["schedule_duration_minutes"], minimum=90) * 60
        post = coerce_int(profile.get("schedule_post_minutes"), DEFAULT_PROFILE["schedule_post_minutes"], minimum=0) * 60
        events = []
        for event in payload.get("events", []) or []:
            start_dt = self._parse_match_datetime(event.get("date"))
            if not start_dt:
                continue
            status = event.get("status") or {}
            status_type = status.get("type") or {}
            state = str(status_type.get("state", ""))
            completed = bool(status_type.get("completed"))
            if completed:
                continue
            name, short_name = self._event_name(event)
            event_id = str(event.get("id") or event.get("uid") or f"{short_name}-{start_dt.timestamp()}")
            start_ts = start_dt.timestamp() - pre
            end_ts = start_dt.timestamp() + duration + post
            events.append(MatchEvent(event_id, name, short_name, start_ts, end_ts, state, completed))
        return events

    def refresh_schedule(self, force=False):
        profile = self.get_profile()
        if coerce_text(profile.get("schedule_provider"), "espn").lower() != "espn":
            raise RuntimeError("only espn schedule provider is supported")
        now_ts = time.time()
        refresh_hours = coerce_int(profile.get("schedule_refresh_hours"), DEFAULT_PROFILE["schedule_refresh_hours"], minimum=1)
        with self.lock:
            if not force and self.schedule_events and now_ts - self.schedule_last_refresh < refresh_hours * 3600:
                return self._schedule_status_snapshot()

        events_by_id = {}
        errors = []
        for date_value in self._utc_date_range():
            url = self._schedule_url(profile, date_value)
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "live-sync-webui/1.0"})
                with urllib.request.urlopen(req, timeout=20) as resp:
                    payload = json.loads(resp.read().decode("utf-8", errors="replace"))
                for event in self._parse_schedule_events(payload, profile):
                    events_by_id[event.event_id] = event
            except Exception as exc:
                errors.append(f"{date_value}: {exc}")

        events = sorted(events_by_id.values(), key=lambda item: item.start_ts)
        with self.lock:
            self.schedule_events = events
            self.schedule_last_refresh = now_ts
            message = f"loaded {len(events)} scheduled matches"
            if errors and not events:
                message = "schedule refresh failed: " + "; ".join(errors[:3])
            elif errors:
                message += f" ({len(errors)} date errors)"
            self.status["schedule"]["last_refresh"] = now()
            self.status["schedule"]["message"] = message
        self.log(f"schedule: {message}")
        return self._schedule_status_snapshot()

    def _match_to_dict(self, match, profile):
        if not match:
            return None
        tz = self._schedule_tz(profile)
        start = datetime.fromtimestamp(match.start_ts, timezone.utc).astimezone(tz)
        end = datetime.fromtimestamp(match.end_ts, timezone.utc).astimezone(tz)
        return {
            "event_id": match.event_id,
            "name": match.name,
            "short_name": match.short_name,
            "window_start": start.isoformat(timespec="minutes"),
            "window_end": end.isoformat(timespec="minutes"),
            "state": match.state,
        }

    def _active_and_next_match(self, profile):
        now_ts = time.time()
        events = list(self.schedule_events)
        active = None
        for event in events:
            if event.start_ts <= now_ts <= event.end_ts:
                active = event
                break
        next_match = None
        for event in events:
            if event.end_ts >= now_ts:
                if active and event.event_id == active.event_id:
                    continue
                next_match = event
                break
        return active, next_match

    def _schedule_status_snapshot(self):
        profile = self.profile.copy()
        enabled = parse_bool(profile.get("schedule_enabled", DEFAULT_PROFILE.get("schedule_enabled", True)))
        active, next_match = self._active_and_next_match(profile)
        return {
            "enabled": enabled,
            "provider": profile.get("schedule_provider", DEFAULT_PROFILE["schedule_provider"]),
            "league": profile.get("schedule_league", DEFAULT_PROFILE["schedule_league"]),
            "timezone": profile.get("schedule_timezone", DEFAULT_PROFILE["schedule_timezone"]),
            "active": active is not None,
            "active_match": self._match_to_dict(active, profile),
            "next_match": self._match_to_dict(next_match, profile),
            "event_count": len(self.schedule_events),
            "last_refresh": self.status.get("schedule", {}).get("last_refresh"),
            "message": self.status.get("schedule", {}).get("message", ""),
            "manual_override": bool(self.manual_override_until_event_id),
            "owned_run": bool(self.schedule_owned_run),
        }

    def _auto_align_allowed_by_schedule(self, profile):
        if parse_bool(profile.get("auto_align_debug_override", DEFAULT_PROFILE.get("auto_align_debug_override", False))):
            return True
        if not parse_bool(profile.get("schedule_enabled", DEFAULT_PROFILE.get("schedule_enabled", True))):
            return True
        with self.lock:
            has_events = bool(self.schedule_events)
            schedule_last_refresh = self.schedule_last_refresh
        if not has_events:
            return schedule_last_refresh <= 0
        active, _next_match = self._active_and_next_match(profile)
        return active is not None

    def start_scheduler(self):
        with self.lock:
            if self.schedule_thread and self.schedule_thread.is_alive():
                return
            self.schedule_stop_event.clear()
            self.schedule_thread = threading.Thread(target=self._schedule_loop, daemon=True)
            self.schedule_thread.start()
        self.log("schedule: automation thread started")

    def stop_scheduler(self):
        self.schedule_stop_event.set()
        thread = self.schedule_thread
        if thread and thread.is_alive():
            thread.join(timeout=2)

    def _schedule_loop(self):
        while not self.schedule_stop_event.is_set():
            profile = self.get_profile()
            enabled = parse_bool(profile.get("schedule_enabled", DEFAULT_PROFILE.get("schedule_enabled", True)))
            if not enabled:
                with self.lock:
                    self.status["schedule"]["enabled"] = False
                    self.status["schedule"]["active"] = False
                    self.status["schedule"]["message"] = "schedule disabled"
                self.schedule_stop_event.wait(30)
                continue
            try:
                self.refresh_schedule(force=False)
                self._apply_schedule(profile)
            except Exception as exc:
                msg = f"schedule error: {exc}"
                self.log(msg)
                with self.lock:
                    self.status["schedule"]["message"] = msg
            poll = coerce_int(profile.get("schedule_poll_seconds"), DEFAULT_PROFILE["schedule_poll_seconds"], minimum=30)
            self.schedule_stop_event.wait(poll)

    def _apply_schedule(self, profile):
        active, next_match = self._active_and_next_match(profile)
        with self.lock:
            running = bool(self.status.get("running"))
            self.status["schedule"]["enabled"] = True
            self.status["schedule"]["active"] = active is not None
            self.status["schedule"]["active_match"] = self._match_to_dict(active, profile)
            self.status["schedule"]["next_match"] = self._match_to_dict(next_match, profile)
            self.status["schedule"]["manual_override"] = bool(self.manual_override_until_event_id)
            self.status["schedule"]["owned_run"] = bool(self.schedule_owned_run)

        if active:
            with self.lock:
                if active.event_id != self.schedule_last_active_id:
                    self.schedule_last_active_id = active.event_id
                    if self.manual_override_until_event_id != active.event_id:
                        self.manual_override_until_event_id = ""
                blocked = self.manual_override_until_event_id == active.event_id
            if not running and not blocked:
                self.log(f"schedule: starting for {active.short_name}")
                self.start(source="schedule")
            elif blocked:
                with self.lock:
                    self.status["schedule"]["message"] = f"manual stop holds until {active.short_name} window ends"
            return

        with self.lock:
            if self.manual_override_until_event_id and self.manual_override_until_event_id == self.schedule_last_active_id:
                self.manual_override_until_event_id = ""
            owned = self.schedule_owned_run
        if running and owned:
            self.log("schedule: stopping outside match window")
            self.stop(source="schedule")

    def warm_channel_cache(self):
        profile = self.get_profile()
        urls = []
        for key, local_key in (("video_playlist", "video_local_m3u"), ("audio_playlist", "audio_local_m3u")):
            if profile.get(local_key):
                continue
            for url in split_lines(profile.get(key, "")):
                if url and url not in urls:
                    urls.append(url)
        if not urls:
            return

        def worker():
            for url in urls:
                try:
                    self.resolver.fetch(url, force=True)
                except Exception as exc:
                    self.log(f"channel cache warm failed for {_redact_url(url)}: {_redact_url(exc)}")

        threading.Thread(target=worker, daemon=True).start()

    # ── Auto-alignment ──────────────────────────────────────────────

    def _extract_frame(self, url, at, out_path, timeout, headers=None):
        subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-rw_timeout", str(timeout * 1_000_000),
            *self._http_input_options(url, headers),
            "-i", url, "-ss", f"{at:.3f}",
            "-frames:v", "1", "-update", "1", str(out_path),
        ], capture_output=True, timeout=timeout + 10)

    def _capture_sampled_frame(self, url, at, out_path, timeout, headers, barrier, deadline_ns):
        barrier.wait()
        now_ns = time.monotonic_ns()
        if deadline_ns and now_ns < deadline_ns:
            time.sleep((deadline_ns - now_ns) / 1_000_000_000)
        started_at = time.monotonic()
        self._extract_frame(url, at, out_path, timeout, headers)
        finished_at = time.monotonic()
        return FrameCaptureResult(Path(out_path), "", started_at, finished_at, media_time=float(at))

    def _record_alignment_clip(self, url, out_path, duration, timeout, headers=None):
        attempts = [
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-rw_timeout", str(timeout * 1_000_000),
                *self._http_input_options(url, headers),
                *self._local_hls_input_options(url, -1),
                "-i", url,
                "-t", f"{duration:.3f}",
                "-map", "0:v:0",
                "-an",
                "-c:v", "copy",
                "-avoid_negative_ts", "make_zero",
                "-f", "matroska",
                str(out_path),
            ],
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-rw_timeout", str(timeout * 1_000_000),
                *self._http_input_options(url, headers),
                *self._local_hls_input_options(url, -1),
                "-i", url,
                "-t", f"{duration:.3f}",
                "-map", "0:v:0",
                "-an",
                "-vf", "scale='min(1280,iw)':-2",
                "-c:v", "mjpeg",
                "-q:v", "3",
                "-f", "matroska",
                str(out_path),
            ],
        ]
        errors = []
        for cmd in attempts:
            out_path.unlink(missing_ok=True)
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + duration + 10)
            if proc.returncode == 0 and out_path.exists() and out_path.stat().st_size > 0:
                return
            errors.append(proc.stderr.strip() or f"empty clip: {out_path}")
        raise RuntimeError(errors[-1] if errors else f"empty clip: {out_path}")

    def _parse_clock_text(self, text):
        normalized = self._normalize_ocr_text(text)
        cleaned = re.sub(r"\s+", "", normalized)

        parsed = self._parse_stoppage_ocr_text(normalized)
        if parsed:
            return parsed

        # Try ST:X:Y+Z (e.g. 94:32+8). Short elapsed stoppage forms are handled above.
        m = CLOCK_WITH_ADDED_RE.search(cleaned)
        if m:
            mins, secs, added = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if secs < 60 and mins <= 150 and mins not in ADJACENT_STOPPAGE_BASES and added <= 30:
                return ClockParse(mins * 60 + secs, f"{m.group(1)}:{m.group(2)}+{added}", "stoppage")

        # Try STOPPAGE_SEPARATE_RE (e.g. 4:32 mins.+8) with spaces preserved.
        cleaned2 = re.sub(r"\s+", " ", normalized).strip()
        m = STOPPAGE_SEPARATE_RE.search(cleaned2)
        if m:
            mins, secs, added = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if secs < 60 and mins <= 150 and mins not in ADJACENT_STOPPAGE_BASES and added <= 30:
                return ClockParse(mins * 60 + secs, f"{m.group(1)}:{m.group(2)}+{added}", "stoppage")

        # Simple clock (e.g. 90:00)
        if "+" in cleaned:
            return None
        m = CLOCK_RE.search(cleaned)
        if not m:
            return None
        mins, secs = int(m.group(1)), int(m.group(2))
        if secs >= 60 or mins > 150:
            return None
        return ClockParse(mins * 60 + secs, m.group(0), "clock")

    def _normalize_ocr_text(self, text):
        return (text or "").strip().replace("O", "0").replace("o", "0").replace("＋", "+")

    def _parse_stoppage_ocr_text(self, text):
        normalized = self._normalize_ocr_text(text)
        cleaned = re.sub(r"\s+", "", normalized)

        m = STOPPAGE_RE.search(cleaned)
        if m:
            parsed = self._combine_stoppage_parts(m.group(1), "00", m.group(2), m.group(3), m.group(0))
            if parsed:
                return parsed

        # Scoreboards often OCR as either "45:00 0:32+4" or three separate lines:
        # "45:00", "0:32", "+4". The elapsed timer is the actual game time offset.
        clocks = []
        for m in CLOCK_RE.finditer(normalized):
            mins = int(m.group(1))
            secs = int(m.group(2))
            if secs < 60 and mins <= 150:
                clocks.append({"minute": mins, "second": secs, "text": m.group(0), "start": m.start(), "end": m.end()})
        bases = [item for item in clocks if item["minute"] in ADJACENT_STOPPAGE_BASES and item["second"] <= 5]
        elapsed_candidates = [item for item in clocks if item["minute"] <= 30 and item["second"] < 60]
        added_matches = []
        for m in ADDED_TIME_RE.finditer(normalized):
            added_min = int(m.group(1))
            added_sec = int(m.group(2) or "0")
            if added_min <= 30 and added_sec < 60:
                added_matches.append({"minute": added_min, "second": added_sec, "text": m.group(0), "start": m.start(), "end": m.end()})

        for base in bases:
            later_elapsed = [item for item in elapsed_candidates if item["start"] > base["end"]]
            later_elapsed.sort(key=lambda item: item["start"])
            for elapsed in later_elapsed:
                later_added = [item for item in added_matches if item["start"] >= elapsed["end"]]
                if later_added and elapsed["minute"] > later_added[0]["minute"]:
                    continue
                label = f"{base['text']}+{elapsed['text']}"
                if later_added:
                    label = f"{label}{later_added[0]['text']}"
                return self._combine_elapsed_stoppage_parts(base["minute"], base["second"], elapsed["minute"], elapsed["second"], label)

        lines = [line.strip() for line in normalized.splitlines() if line.strip()]
        base = None
        elapsed = None
        added = None
        for line in lines:
            if base is None:
                base = self._parse_stoppage_base_text(line)
                if base:
                    continue
            if elapsed is None:
                elapsed = self._parse_elapsed_added_time_text(line)
                if elapsed:
                    continue
            if added is None:
                added = self._parse_added_time_text(line)
        if base and elapsed:
            label = f"{base['text']}+{elapsed['text']}"
            if added:
                label = f"{label}{added['text']}"
            return self._combine_elapsed_stoppage_parts(base["minute"], base["second"], elapsed["minute"], elapsed["second"], label)

        return None

    def _parse_stoppage_base_text(self, text):
        cleaned = re.sub(r"\s+", "", self._normalize_ocr_text(text))
        m = STOPPAGE_BASE_RE.search(cleaned)
        if not m:
            return None
        base_min = int(m.group(1))
        base_sec = int(m.group(2) or "0")
        if base_min not in ADJACENT_STOPPAGE_BASES or base_sec > 5:
            return None
        return {"minute": base_min, "second": base_sec, "text": m.group(0)}

    def _parse_added_time_text(self, text):
        cleaned = re.sub(r"\s+", "", self._normalize_ocr_text(text))
        m = ADDED_TIME_RE.search(cleaned)
        if not m:
            m = ADDED_LINE_RE.search(cleaned)
        noisy_sec = None
        if not m:
            noisy_sec = re.search(r"^\+?([0-9]{1,2})[:：.]([0-5][0-9])[0-9]+$", cleaned)
            if not noisy_sec:
                return None
            m = noisy_sec
        added_min = int(m.group(1))
        added_sec = int(m.group(2) or "0")
        if added_min > 30 or added_sec >= 60:
            return None
        if noisy_sec:
            text = f"+{added_min:02d}:{added_sec:02d}"
        else:
            text = "+" + m.group(1) + (f":{m.group(2)}" if m.group(2) else "")
        return {"minute": added_min, "second": added_sec, "text": text}

    def _parse_elapsed_added_time_text(self, text):
        cleaned = re.sub(r"\s+", "", self._normalize_ocr_text(text))
        m = ELAPSED_ADDED_TIME_RE.search(cleaned)
        if not m:
            return None
        elapsed_min = int(m.group(1))
        elapsed_sec = int(m.group(2))
        if elapsed_min > 30 or elapsed_sec >= 60:
            return None
        return {"minute": elapsed_min, "second": elapsed_sec, "text": f"{elapsed_min}:{elapsed_sec:02d}"}

    def _combine_stoppage_parts(self, base_min, base_sec, added_min, added_sec, text=None):
        base_min = int(base_min)
        base_sec = int(base_sec or 0)
        added_min = int(added_min)
        added_sec = int(added_sec or 0)
        if base_min not in ADJACENT_STOPPAGE_BASES or base_sec > 5:
            return None
        if added_min > 30 or added_sec >= 60:
            return None
        label = text or f"{base_min:02d}:00+{added_min:02d}:{added_sec:02d}"
        return ClockParse((base_min + added_min) * 60 + added_sec, label, "stoppage")

    def _combine_elapsed_stoppage_parts(self, base_min, base_sec, elapsed_min, elapsed_sec, text=None):
        base_min = int(base_min)
        base_sec = int(base_sec or 0)
        elapsed_min = int(elapsed_min)
        elapsed_sec = int(elapsed_sec or 0)
        if base_min not in ADJACENT_STOPPAGE_BASES or base_sec > 5:
            return None
        if elapsed_min > 30 or elapsed_sec >= 60:
            return None
        label = text or f"{base_min:02d}:00+{elapsed_min}:{elapsed_sec:02d}"
        return ClockParse(base_min * 60 + elapsed_min * 60 + elapsed_sec, label, "stoppage")

    def _ocr_region_with_provider(self, provider, frame_path, roi, profile):
        if provider == "ocrspace":
            return self._ocr_via_ocrspace(frame_path, roi, profile)
        if provider == "custom":
            return self._ocr_via_custom(frame_path, roi, profile)
        return None

    def _ocrspace_candidate_rois(self, roi, profile):
        candidates = []
        try:
            base = parse_roi(roi)
        except (TypeError, ValueError):
            base = None
        if base:
            candidates.append(base)
            candidates.append(self._timer_preview_roi(base))
            candidates.append(self._scoreboard_scan_roi("left", width=max(0.5, min(1.0, base[2] * 6)), height=max(0.25, min(1.0, base[3] * 6))))
        candidates.extend([
            self._scoreboard_scan_roi("left"),
            self._scoreboard_scan_roi("center"),
            self._scoreboard_scan_roi("right"),
            (0.0, 0.0, 1.0, 0.5),
            (0.0, 0.0, 1.0, 1.0),
        ])
        seen = set()
        result = []
        for item in candidates:
            key = tuple(round(float(part), 4) for part in item)
            if key in seen:
                continue
            seen.add(key)
            result.append(item)
        return result

    def _dedupe_rois(self, rois):
        seen = set()
        result = []
        for item in rois:
            try:
                roi = parse_roi(",".join(str(part) for part in item)) if isinstance(item, (list, tuple)) else parse_roi(item)
            except (TypeError, ValueError):
                continue
            key = tuple(round(float(part), 4) for part in roi)
            if key in seen:
                continue
            seen.add(key)
            result.append(roi)
        return result

    def _prepare_ocrspace_crop(self, img, roi):
        h, w = img.shape[:2]
        x, y, rw, rh = roi
        crop = img[int(y*h):int((y+rh)*h), int(x*w):int((x+rw)*w)]
        if crop.size == 0:
            return None
        if crop.shape[1] < 960:
            scale = max(1.0, 960.0 / max(crop.shape[1], 1))
            crop = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        norm = cv2.normalize(gray, None, 0, 255, cv2.NORM_MINMAX)
        if min(norm.shape[:2]) < 240:
            norm = cv2.resize(norm, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
        proc = cv2.cvtColor(norm, cv2.COLOR_GRAY2BGR)
        ok, buf = cv2.imencode(".jpg", proc, [int(cv2.IMWRITE_JPEG_QUALITY), 95])
        return buf if ok else None

    def _parse_ocr_text_candidates(self, raw_text):
        text = str(raw_text or "").strip()
        if not text:
            return None
        if self._ocr_reply_says_no_scoreboard(text):
            return None
        if not self._ocr_reply_has_scoreboard_features(text):
            return None
        parsed = self._parse_clock_text(text)
        if parsed:
            return parsed
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        for line in lines:
            if self._ocr_reply_says_no_scoreboard(line):
                continue
            parsed = self._parse_clock_text(line)
            if parsed:
                return parsed
        return None

    def _ocr_reply_says_no_scoreboard(self, text):
        normalized = re.sub(r"[^a-z]+", "", str(text or "").strip().lower())
        return normalized in {
            "none",
            "null",
            "empty",
            "unknown",
            "noscoreboard",
            "nosoccerscoreboard",
            "nofootballscoreboard",
            "nobroadcastscoreboard",
            "notimer",
        }

    def _ocr_reply_has_scoreboard_features(self, text):
        raw = str(text or "").strip()
        if not raw:
            return False
        compact = re.sub(r"\s+", " ", raw)
        score_pair = None
        for match in re.finditer(r"(?<!\d)(\d{1,2})\s*[-:]\s*(\d{1,2})(?!\d)", compact):
            left = int(match.group(1))
            right = int(match.group(2))
            if left <= 20 and right <= 20:
                score_pair = match
                break
        team_blocks = re.findall(r"\b[A-Z]{2,4}\b", compact)
        versus = re.search(r"\b[A-Z][A-Za-z]{1,12}\s+(?:v|vs|VS)\.?\s+[A-Z][A-Za-z]{1,12}\b", compact)
        has_time = False
        for line in [compact, *[line.strip() for line in compact.splitlines() if line.strip()]]:
            if self._parse_clock_text(line):
                has_time = True
                break
        has_score = score_pair is not None
        has_team_like = len(team_blocks) >= 2 or versus is not None
        return has_time and (has_score or has_team_like)

    def _is_stoppage_base_clock_text(self, text):
        parsed = self._parse_clock_text(text)
        if not parsed or parsed.kind != "clock":
            return False
        mins = int(parsed.game_time // 60)
        secs = int(parsed.game_time % 60)
        return mins in ADJACENT_STOPPAGE_BASES and secs <= 5

    def _expand_roi(self, roi, *, width_scale=1.0, height_scale=1.0, shift_x=0.0, shift_y=0.0):
        x, y, w, h = parse_roi(",".join(str(part) for part in roi)) if isinstance(roi, (list, tuple)) else parse_roi(roi)
        cx = x + w / 2 + shift_x * w
        cy = y + h / 2 + shift_y * h
        new_w = min(1.0, w * width_scale)
        new_h = min(1.0, h * height_scale)
        new_x = min(max(0.0, cx - new_w / 2), max(0.0, 1.0 - new_w))
        new_y = min(max(0.0, cy - new_h / 2), max(0.0, 1.0 - new_h))
        return (new_x, new_y, new_w, new_h)

    def _stoppage_retry_rois(self, roi):
        base = parse_roi(",".join(str(part) for part in roi)) if isinstance(roi, (list, tuple)) else parse_roi(roi)
        side_shift = 0.8 if base[0] < 0.5 else -0.8
        candidates = [
            self._expand_roi(base, width_scale=2.8, height_scale=1.8),
            self._expand_roi(base, width_scale=3.6, height_scale=2.2, shift_x=side_shift),
            self._timer_preview_roi(base),
        ]
        return self._dedupe_rois(candidates)

    def _retry_stoppage_with_expanded_roi(self, provider, frame_path, roi, profile):
        for candidate_roi in self._stoppage_retry_rois(roi):
            result = self._ocr_region_with_provider(provider, frame_path, candidate_roi, profile)
            if not result:
                continue
            parsed = self._parse_ocr_text_candidates(result[1])
            if parsed and parsed.kind == "stoppage":
                return parsed.game_time, parsed.text
        return None

    def _custom_ocr_prompt(self):
        return (
            "First decide whether this image contains a football or soccer live-match scoreboard with a match timer. "
            "If there is no football or soccer scoreboard/timer, return ONLY NO_SCOREBOARD. "
            "If it is not a live football scoreboard, return ONLY NO_SCOREBOARD. "
            "Only accept it if the same scoreboard also shows team names/abbreviations or a score such as 0-0, 1:0, ENG, BRA. "
            "If the timer is visible but there are no team names and no score, return ONLY NO_SCOREBOARD. "
            "Otherwise return ONLY the raw scoreboard text that contains the timer together with the team/score context. "
            "Examples: ENG 0-0 BRA 45:00, ARS CHE 90:00+02:30, LIV 1-0 MCI 45:00 0:32+4. No explanation."
        )

    def _custom_scoreboard_side_prompt(self):
        return (
            "This image is the top half of a football broadcast frame split into two equal vertical areas. "
            "Return ONLY 1 if the scoreboard/timer graphic is in the left half. "
            "Return ONLY 2 if it is in the right half. "
            "Return ONLY unknown if no scoreboard/timer graphic is visible."
        )

    def _send_custom_ocr_crop(self, crop_img, endpoint, api_key, model, prompt=None):
        import cv2 as _cv2, base64, json as _json, urllib.request
        if crop_img is None or crop_img.size == 0:
            return None
        _, buf = _cv2.imencode(".jpg", crop_img, [int(_cv2.IMWRITE_JPEG_QUALITY), 85])
        b64 = base64.b64encode(buf).decode("utf-8")
        self._record_ocr_request("custom")
        prompt_text = prompt or self._custom_ocr_prompt()
        request_specs = [
            (
                f"{endpoint}/responses",
                {
                    "model": model,
                    "input": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "input_text", "text": prompt_text},
                                {"type": "input_image", "image_url": f"data:image/jpeg;base64,{b64}"},
                            ],
                        }
                    ],
                    "max_output_tokens": 50,
                },
            ),
            (
                f"{endpoint}/chat/completions",
                {
                    "model": model,
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": prompt_text},
                                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                            ],
                        }
                    ],
                    "max_tokens": 50,
                },
            ),
        ]
        last_exc = None
        for url, payload in request_specs:
            req = urllib.request.Request(
                url,
                data=_json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}", "User-Agent": "live-sync-ocr/1.0"},
            )
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    result = _json.loads(resp.read().decode("utf-8"))
            except Exception as exc:
                last_exc = exc
                continue
            reply = self._extract_custom_ocr_text(result)
            if reply:
                return reply
        if last_exc is not None:
            self.log(f"Custom OCR API error: {last_exc}")
        return None

    def _extract_custom_ocr_text(self, result):
        if not isinstance(result, dict):
            return ""
        output_text = str(result.get("output_text") or "").strip()
        if output_text:
            return output_text
        output = result.get("output") or []
        for item in output:
            for content in item.get("content") or []:
                if content.get("type") in ("output_text", "text"):
                    text = str(content.get("text") or "").strip()
                    if text:
                        return text
        choices = result.get("choices") or []
        for choice in choices:
            text = str((choice.get("message") or {}).get("content") or "").strip()
            if text:
                return text
        return ""

    def _record_ocr_request(self, provider):
        with self.lock:
            self.status["ocr_request_count"] = int(self.status.get("ocr_request_count") or 0) + 1
            self.status["ocr_request_last_at"] = now()
            self.status["ocr_request_last_provider"] = provider

    def _log_ocr_provider_failure(self, provider):
        if provider == "ocrspace":
            self.ocr_provider_cooldowns[provider] = time.time() + OCR_FALLBACK_COOLDOWN_SECONDS
            self.log("OCR.space failed")
        elif provider == "custom":
            self.log("Custom OCR API failed")

    def _scoreboard_side_from_ocrspace_ratio(self, left_ratio):
        if left_ratio is None:
            return None
        try:
            ratio = float(left_ratio)
        except (TypeError, ValueError):
            return None
        if ratio < 0.5:
            return "left"
        if ratio >= 0.5:
            return "right"
        return None

    def _scoreboard_side_from_custom_reply(self, reply):
        normalized = re.sub(r"[^0-9a-z]+", "", str(reply or "").strip().lower())
        if normalized in ("1", "left"):
            return "left"
        if normalized in ("2", "right"):
            return "right"
        return None

    def _detect_scoreboard_side_via_custom(self, img, endpoint, api_key, model):
        roi = self._scoreboard_top_half_roi()
        h, w = img.shape[:2]
        x, y, rw, rh = roi
        crop = img[int(y*h):int((y+rh)*h), int(x*w):int((x+rw)*w)]
        reply = self._send_custom_ocr_crop(crop, endpoint, api_key, model, prompt=self._custom_scoreboard_side_prompt())
        return self._scoreboard_side_from_custom_reply(reply)

    def _ocr_time(self, frame_path, roi, scale=6):
        profile = self.get_profile()
        providers = ocr_provider_order(profile)
        if not providers or not ocr_provider_ready_for(providers[0], profile):
            return None
        for idx, provider in enumerate(providers):
            if idx > 0 and not ocr_provider_ready_for(provider, profile):
                continue
            cooldown_until = float(self.ocr_provider_cooldowns.get(provider, 0) or 0)
            if idx > 0 and cooldown_until > time.time():
                continue
            if idx > 0:
                self.log(f"OCR primary '{providers[0]}' failed, ROI fallback to '{provider}'")
            result = self._ocr_region_with_provider(provider, frame_path, roi, profile)
            if result:
                parsed_text = self._parse_ocr_text_candidates(result[1])
                if not parsed_text:
                    self._log_ocr_provider_failure(provider)
                    continue
                normalized_result = (parsed_text.game_time, parsed_text.text)
                if self._is_stoppage_base_clock_text(normalized_result[1]):
                    retried = self._retry_stoppage_with_expanded_roi(provider, frame_path, roi, profile)
                    if retried:
                        return retried
                return normalized_result
            self._log_ocr_provider_failure(provider)
        return None


    def _ocr_send_ocrspace_jpeg(self, jpeg_buf, api_key):
        """Send a JPEG buffer to OCR.space, return (text, left_ratio) or None."""
        import json as _json, urllib.request
        self._record_ocr_request("ocrspace")
        jpeg_data = jpeg_buf.tobytes() if hasattr(jpeg_buf, "tobytes") else bytes(jpeg_buf)
        boundary = f"----ocrspace{uuid.uuid4().hex}"
        body = bytearray()
        crlf = b"\r\n"

        def add_field(name, value):
            body.extend(f"--{boundary}\r\n".encode("utf-8"))
            body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
            body.extend(str(value).encode("utf-8"))
            body.extend(crlf)

        def add_file(name, filename, content_type, data):
            body.extend(f"--{boundary}\r\n".encode("utf-8"))
            body.extend(f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode("utf-8"))
            body.extend(f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"))
            body.extend(data)
            body.extend(crlf)

        add_field("apikey", api_key)
        add_field("language", "eng")
        add_field("isOverlayRequired", "true")
        add_field("OCREngine", "2")
        add_field("detectOrientation", "false")
        add_field("scale", "false")
        add_file("file", "frame.jpg", "image/jpeg", jpeg_data)
        body.extend(f"--{boundary}--\r\n".encode("utf-8"))
        req = urllib.request.Request(
            "https://api.ocr.space/parse/image",
            data=bytes(body),
            headers={
                "apikey": api_key,
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Accept": "application/json",
                "User-Agent": "live-sync-ocr/1.0",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                result = _json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            self.log(f"OCR.space API error: {exc}")
            return None
        if result.get("IsErroredOnProcessing") or result.get("OCRExitCode") != 1:
            err = result.get("ErrorMessage", ["unknown"])[0] if isinstance(result.get("ErrorMessage"), list) else str(result.get("ErrorMessage", "unknown"))
            self.log(f"OCR.space processing error: {err}")
            return None
        parsed_items = result.get("ParsedResults") or []
        if not parsed_items:
            return None
        parsed_text = (parsed_items[0].get("ParsedText") or "").strip()
        if not parsed_text:
            return None
        left_ratio = None
        overlay = parsed_items[0].get("TextOverlay") or {}
        lines = overlay.get("Lines") or []
        img_w = float(overlay.get("ImageWidth", 1) or 1)

        def line_center_ratio(words):
            boxes = []
            for word in words or []:
                try:
                    left = float(word.get("Left", 0) or 0)
                    width = float(word.get("Width", 0) or 0)
                except (TypeError, ValueError):
                    continue
                boxes.append((left, left + width))
            if not boxes:
                return None
            return ((min(item[0] for item in boxes) + max(item[1] for item in boxes)) / 2) / max(img_w, 1)

        fallback_ratio = None
        for line in lines:
            words = line.get("Words") or []
            ratio = line_center_ratio(words)
            if ratio is not None and fallback_ratio is None:
                fallback_ratio = ratio
            line_text = " ".join(str(word.get("WordText", "")).strip() for word in words).strip()
            if ratio is not None and self._parse_ocr_text_candidates(line_text):
                left_ratio = ratio
                break
        if left_ratio is None:
            left_ratio = fallback_ratio
        return parsed_text, left_ratio

    def _ocr_via_ocrspace(self, frame_path, roi, profile):
        """Send one or more crops to OCR.space and return parsed clock."""
        api_key = coerce_text(profile.get("ocrspace_api_key", "")).strip()
        if not api_key:
            return None
        img = cv2.imread(str(frame_path))
        if img is None:
            return None
        for candidate_roi in self._ocrspace_candidate_rois(roi, profile):
            buf = self._prepare_ocrspace_crop(img, candidate_roi)
            if buf is None:
                continue
            result = self._ocr_send_ocrspace_jpeg(buf, api_key)
            if result is None:
                continue
            raw_text, _left_ratio = result
            parsed = self._parse_ocr_text_candidates(raw_text)
            if parsed:
                return parsed.game_time, parsed.text
        return None

    def _find_clock_via_ocrspace(self, frame_path, profile):
        """Read the timer only from the top quarter of the frame."""
        api_key = coerce_text(profile.get("ocrspace_api_key", "")).strip()
        if not api_key:
            return None
        img = cv2.imread(str(frame_path))
        if img is None:
            return None
        top_quarter_roi = self._scoreboard_top_quarter_roi()
        buf = self._prepare_ocrspace_crop(img, top_quarter_roi)
        if buf is None:
            return None
        result = self._ocr_send_ocrspace_jpeg(buf, api_key)
        if result is None:
            return None
        raw_text, _left_ratio = result
        parsed = self._parse_ocr_text_candidates(raw_text)
        if parsed:
            return parsed.game_time, parsed.text, top_quarter_roi
        return None

    def _ocr_via_custom(self, frame_path, roi, profile):
        """Send cropped region to custom OpenAI-compatible API and return parsed clock."""
        endpoint = coerce_text(profile.get("ocr_custom_endpoint", "")).strip().rstrip("/")
        api_key = coerce_text(profile.get("ocr_api_key", "")).strip()
        model = coerce_text(profile.get("ocr_custom_model", DEFAULT_PROFILE.get("ocr_custom_model", "gpt-4o"))).strip()
        if not api_key or not endpoint:
            return None
        img = cv2.imread(str(frame_path))
        if img is None:
            return None
        h, w = img.shape[:2]
        x, y, rw, rh = roi
        crop = img[int(y*h):int((y+rh)*h), int(x*w):int((x+rw)*w)]
        reply = self._send_custom_ocr_crop(crop, endpoint, api_key, model)
        if not reply:
            return None
        parsed = self._parse_ocr_text_candidates(reply)
        if parsed:
            return parsed.game_time, parsed.text
        return None

    def _test_ocr_provider(self, profile):
        provider = normalize_ocr_provider(profile.get("ocr_provider"))
        if not provider:
            return {"ok": False, "provider": "", "message": "OCR 服务商未配置"}
        if not ocr_provider_ready_for(provider, profile):
            missing = []
            key_name = "ocr_api_key" if provider == "custom" else "ocrspace_api_key"
            if not coerce_text(profile.get(key_name, "")).strip():
                missing.append("OCR API Key" if provider == "custom" else "OCR.space API Key")
            if provider == "custom" and not coerce_text(profile.get("ocr_custom_endpoint", "")).strip():
                missing.append("自定义 OCR 端点")
            if missing:
                return {"ok": False, "provider": provider, "message": f"缺少配置: {', '.join(missing)}"}
            return {"ok": False, "provider": provider, "message": "OCR 配置不完整"}

        width, height = 1280, 720
        img = np.full((height, width, 3), 255, dtype=np.uint8)
        cv2.rectangle(img, (20, 18), (620, 228), (26, 26, 26), -1)
        cv2.putText(img, "90:00", (52, 104), cv2.FONT_HERSHEY_SIMPLEX, 2.4, (255, 255, 255), 5, cv2.LINE_AA)
        cv2.putText(img, "+02:30", (52, 188), cv2.FONT_HERSHEY_SIMPLEX, 2.0, (255, 255, 255), 4, cv2.LINE_AA)
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        try:
            cv2.imwrite(str(tmp_path), img, [int(cv2.IMWRITE_JPEG_QUALITY), 95])
            messages = []
            success = False
            for idx, current in enumerate(ocr_provider_order(profile)):
                label = ocr_provider_label(current)
                if idx > 0 and not ocr_provider_ready_for(current, profile):
                    messages.append(f"备用服务({label}) 未配置")
                    continue
                try:
                    if current == "ocrspace":
                        result = self._find_clock_via_ocrspace(tmp_path, profile)
                    else:
                        result = self._ocr_via_custom(tmp_path, (0.0, 0.0, 1.0, 1.0), profile)
                except Exception as exc:
                    result = None
                    messages.append(f"{label} 异常: {str(exc)[:200]}")
                if result:
                    messages.append(f"{label} 识别成功: {result[1]}")
                    success = True
                    if idx > 0:
                        messages.append(f"已使用备用服务 {label}")
                    break
                if not result:
                    messages.append(f"{label} 识别失败")
            return {"ok": success, "provider": provider, "message": " | ".join(messages) if messages else "OCR 测试失败"}
        finally:
            tmp_path.unlink(missing_ok=True)

    def _find_clock_via_custom(self, frame_path, profile):
        """Read the timer only from the top quarter of the frame."""
        endpoint = coerce_text(profile.get("ocr_custom_endpoint", "")).strip().rstrip("/")
        api_key = coerce_text(profile.get("ocr_api_key", "")).strip()
        model = coerce_text(profile.get("ocr_custom_model", DEFAULT_PROFILE.get("ocr_custom_model", "gpt-4o"))).strip()
        if not api_key or not endpoint:
            return None
        img = cv2.imread(str(frame_path))
        if img is None:
            return None
        h, w = img.shape[:2]
        def _crop_from_roi(roi):
            x, y, rw, rh = roi
            x0 = int(x * w)
            y0 = int(y * h)
            x1 = int((x + rw) * w)
            y1 = int((y + rh) * h)
            crop = img[y0:y1, x0:x1]
            return crop if crop.size else None

        def _parse_roi(roi):
            reply = self._send_custom_ocr_crop(_crop_from_roi(roi), endpoint, api_key, model)
            if not reply:
                return None
            return self._parse_ocr_text_candidates(reply)
        top_quarter_roi = self._scoreboard_top_quarter_roi()
        parsed_roi = _parse_roi(top_quarter_roi)
        if parsed_roi:
            return parsed_roi.game_time, parsed_roi.text, top_quarter_roi
        return None


    def _roi_crop(self, frame_path, roi):
        img = cv2.imread(str(frame_path))
        if img is None:
            return None
        h, w = img.shape[:2]
        x, y, rw, rh = roi
        crop = img[int(y*h):int((y+rh)*h), int(x*w):int((x+rw)*w)]
        return crop if crop.size else None

    def _roi_from_box(self, left, top, width, height, frame_w, frame_h, pad=0.35):
        x0 = max(0, left - width * pad)
        y0 = max(0, top - height * pad)
        x1 = min(frame_w, left + width * (1 + pad))
        y1 = min(frame_h, top + height * (1 + pad))
        if x1 <= x0 or y1 <= y0:
            return None
        return (x0 / frame_w, y0 / frame_h, (x1 - x0) / frame_w, (y1 - y0) / frame_h)

    def _clock_candidate_roi_plausible(self, roi, parsed=None):
        if not roi:
            return False
        x, y, w, h = roi
        if x < 0 or y < 0 or w <= 0 or h <= 0 or x + w > 1.001 or y + h > 1.001:
            return False
        if self._is_scoreboard_top_scan_roi(roi):
            return True
        if w < 0.012 or h < 0.010:
            return False
        if w > 0.24 or h > 0.20 or w * h > 0.035:
            return False
        ratio = w / h
        if ratio < 0.35 or ratio > 10:
            return False
        text = getattr(parsed, "text", "") if parsed else ""
        if "." in text and ":" not in text and "：" not in text and "+" not in text:
            # Dot-only OCR hits are common on signs and jersey graphics. Accept them
            # only when the detected box is still compact and near scoreboard areas.
            if w > 0.12 or h > 0.10 or y > 0.45:
                return False
        return True

    def _collect_clock_samples(self, url, roi, start, end, step, workdir, label, timeout, *, auto_find=False, headers=None):
        samples = []
        tasks = {}
        barrier = threading.Barrier(1)
        deadline_ns = 0
        with ThreadPoolExecutor(max_workers=4) as pool:
            t = start
            while t <= end + 1e-6:
                out = workdir / f"{label}_{t:.3f}.jpg"
                tasks[pool.submit(self._capture_sampled_frame, url, t, out, timeout, headers, barrier, deadline_ns)] = (t, out)
                t += step
            for fut in as_completed(tasks):
                t, out = tasks[fut]
                try:
                    fut.result()
                    parsed = self._ocr_time(out, roi)
                    if parsed:
                        samples.append(ClockSample(float(t), parsed[0], parsed[1], roi))
                    elif auto_find:
                        found = self._find_frame_clock(out, full_frame=True)
                        if found:
                            samples.append(ClockSample(float(t), found.game_time, found.text, found.roi))
                except Exception:
                    pass
        return samples

    def _sample_roi_key(self, sample):
        if not sample.roi:
            return None
        x, y, w, h = sample.roi
        return (round(x / 0.03), round(y / 0.03), round(w / 0.04), round(h / 0.04))

    def _roi_center_distance(self, left, right):
        lx, ly, lw, lh = left
        rx, ry, rw, rh = right
        return math.hypot((lx + lw / 2) - (rx + rw / 2), (ly + lh / 2) - (ry + rh / 2))

    def _filter_samples_near_roi(self, samples, roi, max_distance=0.18):
        return [sample for sample in samples if sample.roi and self._roi_center_distance(sample.roi, roi) <= max_distance]

    def _samples_have_plausible_motion(self, samples, scan_window, step):
        if len(samples) < 2:
            return False
        ordered = sorted(samples, key=lambda sample: sample.media_time)
        times = [sample.game_time for sample in ordered]
        if max(times) - min(times) < self._clock_motion_threshold(scan_window):
            return False
        tolerance = max(1.5, step * 1.2)
        good_pairs = 0
        bad_skew = 0
        for prev, cur in zip(ordered, ordered[1:]):
            media_delta = cur.media_time - prev.media_time
            clock_delta = cur.game_time - prev.game_time
            if clock_delta < -1:
                return False
            if abs(media_delta - step) > step * 0.75:
                bad_skew += 1
            if abs(clock_delta - media_delta) <= tolerance:
                good_pairs += 1
        if bad_skew > max(1, len(ordered) // 3):
            return False
        return good_pairs >= max(1, len(ordered) - 2)

    def _dominant_roi(self, samples):
        grouped = {}
        for sample in samples:
            key = self._sample_roi_key(sample)
            if key is None:
                continue
            grouped.setdefault(key, []).append(sample)
        if not grouped:
            return None, samples
        best = max(grouped.values(), key=len)
        rois = [s.roi for s in best if s.roi]
        roi = tuple(sum(parts) / len(parts) for parts in zip(*rois))
        return roi, best

    def _clock_motion_threshold(self, scan_window):
        return max(1.0, min(5.0, scan_window * 0.6))

    def _maybe_autofind_samples(self, clip, roi, scan_window, step, tmpdir, label, timeout, min_samples, *, allow_relocate=False, preset_rois=None):
        samples = self._collect_clock_samples(str(clip), roi, 0, scan_window, step, tmpdir, label, timeout)
        times = [s.game_time for s in samples]
        motion_threshold = self._clock_motion_threshold(scan_window)
        if len(samples) >= min_samples and times and max(times) - min(times) >= motion_threshold:
            return samples, roi, "configured"
        for idx, preset_roi in enumerate(preset_rois or [], start=1):
            preset_samples = self._collect_clock_samples(str(clip), preset_roi, 0, scan_window, step, tmpdir, f"{label}_preset_{idx}", timeout)
            preset_times = [s.game_time for s in preset_samples]
            if (
                len(preset_samples) >= min_samples
                and preset_times
                and max(preset_times) - min(preset_times) >= motion_threshold
                and self._samples_have_plausible_motion(preset_samples, scan_window, step)
            ):
                return preset_samples, preset_roi, "preset"
        if not allow_relocate:
            return samples, roi, "locked"
        find_window = min(scan_window, step * 2)
        found = self._collect_clock_samples(str(clip), roi, 0, find_window, step, tmpdir, f"{label}_find", timeout, auto_find=True)
        raw_found_count = len(found)
        found_roi, grouped = self._dominant_roi(found)
        if found_roi and grouped and len(grouped) >= min_samples:
            refreshed = self._collect_clock_samples(str(clip), found_roi, 0, scan_window, step, tmpdir, f"{label}_auto", timeout)
            refreshed_times = [s.game_time for s in refreshed]
            if (
                len(refreshed) >= min_samples
                and refreshed_times
                and max(refreshed_times) - min(refreshed_times) >= motion_threshold
                and self._samples_have_plausible_motion(refreshed, scan_window, step)
            ):
                return refreshed, found_roi, "auto"
            self.log(f"auto-align: {label} auto-find rejected unstable clock candidate")
        elif raw_found_count:
            self.log(f"auto-align: {label} auto-find found {raw_found_count} clock candidates but no stable ROI group")
        return samples, roi, "configured"

    def _format_roi_value(self, roi):
        return ",".join(f"{max(0, min(1, part)):.3f}" for part in roi)

    def _save_detected_rois(self, profile, video_roi, audio_roi, video_source, audio_source):
        updates = {}
        if video_source in ("auto", "preset") and video_roi:
            updates["video_roi"] = self._format_roi_value(video_roi)
        if audio_source in ("auto", "preset") and audio_roi:
            updates["audio_roi"] = self._format_roi_value(audio_roi)
        if not updates:
            return
        with self.lock:
            self.profile.update(updates)
            profile.update(updates)
            saved = self.profile.copy()
        json_save(PROFILE_PATH, _strip_url_fields(saved))
        self.log("auto-align: saved detected ROI " + " ".join(f"{k}={v}" for k, v in updates.items()))

    def _save_monitor_rois(self, profile, monitor):
        return

    def _estimate_clock_offset(self, video_samples, audio_samples, profile):
        requested_samples = coerce_int(profile.get("auto_align_samples"), DEFAULT_PROFILE["auto_align_samples"])
        min_samples = max(3, min(requested_samples, math.ceil(requested_samples * 0.6)))
        step = coerce_float(profile.get("auto_align_step"), DEFAULT_PROFILE["auto_align_step"], minimum=0.5)
        motion_threshold = self._clock_motion_threshold((requested_samples - 1) * step)
        max_offset = coerce_float(profile.get("auto_align_max_offset"), DEFAULT_PROFILE["auto_align_max_offset"])
        if len(video_samples) < min_samples:
            return None, f"video {len(video_samples)} < {min_samples}"
        if len(audio_samples) < min_samples:
            return None, f"audio {len(audio_samples)} < {min_samples}"
        v_times = [s.game_time for s in video_samples]
        a_times = [s.game_time for s in audio_samples]
        if max(v_times) - min(v_times) < motion_threshold:
            return None, "video clock static"
        if max(a_times) - min(a_times) < motion_threshold:
            return None, "audio clock static"
        candidates = []
        for vs in video_samples:
            for aus in audio_samples:
                offset = aus.media_time - vs.media_time - (aus.game_time - vs.game_time)
                if abs(offset) <= max_offset:
                    candidates.append(ClockCandidate(abs(offset), offset, vs, aus))
        if not candidates:
            return None, f"no offset within +/-{max_offset}s"
        candidates.sort(key=lambda c: c.diff)
        offsets = [c.offset for c in candidates]
        center = self._offset_cluster_center(offsets)
        if center is None:
            return None, f"no stable offset cluster ({len(candidates)} candidates)"
        cluster = [c for c in candidates if abs(c.offset - center) <= 1.5]
        if len(cluster) < min_samples:
            return None, f"cluster {len(cluster)} < {min_samples}"
        return center, f"aligned ({len(cluster)} matches)"

    def _run_auto_align(self, video_url, audio_url, profile, *, allow_relocate=False):
        video = video_url if isinstance(video_url, Channel) else Channel(name="video", url=video_url)
        audio = audio_url if isinstance(audio_url, Channel) else Channel(name="audio", url=audio_url)
        enabled = ocr_provider_ready(profile)
        if not enabled:
            return None, "disabled"
        sample_count = coerce_int(profile.get("auto_align_samples"), DEFAULT_PROFILE["auto_align_samples"])
        min_samples = max(3, min(sample_count, math.ceil(sample_count * 0.6)))
        step = coerce_float(profile.get("auto_align_step"), DEFAULT_PROFILE["auto_align_step"])
        timeout = coerce_int(profile.get("timeout_seconds"), DEFAULT_PROFILE["timeout_seconds"])
        threshold = coerce_float(profile.get("auto_align_threshold"), DEFAULT_PROFILE["auto_align_threshold"])
        video_roi = parse_roi(profile.get("video_roi", "0.050,0.050,0.070,0.050"))
        audio_roi = parse_roi(profile.get("audio_roi", "0.885,0.085,0.075,0.060"))

        scan_window = (sample_count - 1) * step
        clip_duration = scan_window + 3
        with tempfile.TemporaryDirectory(prefix="align_") as tmp:
            tmpdir = Path(tmp)
            video_clip = tmpdir / "video.ts"
            audio_clip = tmpdir / "audio.ts"
            self.log(f"auto-align: recording {clip_duration:.1f}s clips before OCR")
            with ThreadPoolExecutor(max_workers=2) as pool:
                futs = [
                    pool.submit(self._record_alignment_clip, video.url, video_clip, clip_duration, timeout, video.headers),
                    pool.submit(self._record_alignment_clip, audio.url, audio_clip, clip_duration, timeout, audio.headers),
                ]
                try:
                    for fut in as_completed(futs):
                        fut.result()
                except Exception as exc:
                    msg = f"sample clip failed: {exc}"
                    self.log(f"auto-align: {msg}; keeping current offset")
                    return None, msg
            v_samples, video_roi, video_roi_source = self._maybe_autofind_samples(
                video_clip, video_roi, scan_window, step, tmpdir, "v", timeout, min_samples,
                allow_relocate=allow_relocate,
                preset_rois=parse_roi_list(profile.get("video_roi_presets", "")),
            )
            a_samples, audio_roi, audio_roi_source = self._maybe_autofind_samples(
                audio_clip, audio_roi, scan_window, step, tmpdir, "a", timeout, min_samples,
                allow_relocate=allow_relocate,
                preset_rois=parse_roi_list(profile.get("audio_roi_presets", "")),
            )

        self.log(
            f"auto-align: v={[(s.media_time, s.text) for s in v_samples]} "
            f"a={[(s.media_time, s.text) for s in a_samples]} "
            f"roi=({video_roi_source},{audio_roi_source})"
        )

        offset, msg = self._estimate_clock_offset(v_samples, a_samples, profile)
        if offset is None:
            self.log(f"auto-align: {msg}; keeping current offset")
            with self.lock:
                self.status["auto_align_msg"] = msg
            return None, msg

        current = float(profile.get("offset_seconds", 0))
        self.log(f"auto-align: offset={offset:.3f}s current={current:.3f}s {msg}")

        json_save(OFFSET_STATE, {
            "offset_seconds": round(offset, 3),
            "updated_at_unix": int(time.time()),
            "source": "auto-align"
        })
        with self.lock:
            self.status["offset_seconds"] = offset
            self.status["auto_align_offset_seconds"] = round(offset, 3)
            self.status["auto_align_msg"] = f"offset={offset:.3f}s {msg}"
            self.status["last_alignment"] = time.strftime("%H:%M:%S", time.localtime())
        self._save_detected_rois(profile, video_roi, audio_roi, video_roi_source, audio_roi_source)

        if abs(offset - current) >= threshold:
            self.log(f"auto-align: delta={abs(offset-current):.3f}s >= {threshold}s; restarting")
            return offset, f"new offset {offset:.3f}s"

        return None, f"stable ({offset:.3f}s)"
    def _snapshot_roi_for_kind(self, profile, kind):
        return self._scoreboard_top_quarter_roi()

    def _save_snapshot_from_frame(self, frame_path, kind, profile, source_name):
        with self.snapshot_file_lock:
            SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
            roi = self._snapshot_roi_for_kind(profile, kind)
            parsed = self._ocr_time(frame_path, roi)
            suffix = "timer" if parsed else "full"
            out = SNAPSHOT_DIR / f"{kind}_snapshot.jpg"
            tmp_out = SNAPSHOT_DIR / f".{kind}_snapshot.tmp.jpg"
            crop = self._roi_crop(frame_path, roi)
            if crop is not None:
                cv2.imwrite(str(tmp_out), crop, [int(cv2.IMWRITE_JPEG_QUALITY), 95])
            else:
                shutil.copyfile(frame_path, tmp_out)
                suffix = "full"

            os.replace(tmp_out, out)
            self._prune_snapshots()
            with self.lock:
                self.status["last_snapshot_at"] = now()
                self.status.setdefault("last_ocr_results", {})[kind] = {
                    "clock": parsed[1] if parsed else None,
                    "game_time": parsed[0] if parsed else None,
                    "updated_at": time.strftime("%H:%M:%S", time.localtime()),
                    "error": "" if parsed else "OCR failed",
                }
            detail = parsed[1] if parsed else "full frame"
            self.log(f"captured {kind} {suffix} snapshot: {out.name} ({detail})")
            return {"kind": kind, "url": f"/snapshots/{out.name}", "source": source_name, "mode": suffix, "clock": parsed[1] if parsed else ""}

    def _capture_url_snapshot(self, kind, url, source_name, profile, headers=None):
        if not url:
            raise RuntimeError(f"{kind} URL is empty")
        timeout = coerce_int(profile.get("timeout_seconds"), DEFAULT_PROFILE["timeout_seconds"], minimum=5)
        headers = dict(headers or parse_header_lines(profile.get(f"{kind}_headers", "")))
        tmp_path = self._capture_snapshot_frame(self._direct_input(url, timeout, headers), timeout)
        try:
            return self._save_snapshot_from_frame(tmp_path, kind, profile, source_name)
        finally:
            tmp_path.unlink(missing_ok=True)

    def _capture_snapshot_frame(self, input_args, timeout):
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        try:
            cmd = [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                *input_args, "-frames:v", "1", "-update", "1", str(tmp_path),
            ]
            subprocess.run(cmd, timeout=timeout + 5, check=True)
            return tmp_path
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

    def _runtime_snapshot_jobs(self, video, audio, profile):
        with self.lock:
            jobs = list(self.current_snapshot_jobs)
        if jobs:
            return jobs
        timeout = coerce_int(profile.get("timeout_seconds"), DEFAULT_PROFILE["timeout_seconds"], minimum=5)
        jobs = [("video", self._direct_input(video.url, timeout, video.headers), self.status.get("active_channel") or "active video")]
        if audio and audio.url:
            jobs.append(("audio", self._direct_input(audio.url, timeout, audio.headers), self.status.get("active_audio_channel") or profile.get("audio_channel") or "active audio"))
        return jobs

    def _capture_snapshot_frames(self, jobs, profile):
        jobs = [job for job in jobs if job[1]]
        frames = {}
        errors = {}
        if not jobs:
            return frames, {"snapshot": "no snapshot URLs available"}
        timeout = coerce_int(profile.get("timeout_seconds"), DEFAULT_PROFILE["timeout_seconds"], minimum=5)
        with ThreadPoolExecutor(max_workers=min(2, len(jobs))) as pool:
            futures = {
                pool.submit(self._capture_snapshot_frame, input_args, timeout): (kind, source_name)
                for kind, input_args, source_name in jobs
            }
            for fut in as_completed(futures):
                kind, source_name = futures[fut]
                try:
                    frames[kind] = (fut.result(), source_name)
                except Exception as exc:
                    errors[kind] = str(exc)
        return frames, errors

    def _save_snapshot_frames(self, frames, profile):
        results = {}
        errors = {}
        for kind in SNAPSHOT_KINDS:
            if kind not in frames:
                continue
            frame_path, source_name = frames[kind]
            try:
                results[kind] = self._save_snapshot_from_frame(frame_path, kind, profile, source_name)
            except Exception as exc:
                errors[kind] = str(exc)
        return results, errors

    def _cleanup_snapshot_frames(self, frames):
        for frame_path, _source_name in frames.values():
            frame_path.unlink(missing_ok=True)

    def _capture_snapshot_jobs(self, jobs, profile):
        frames, errors = self._capture_snapshot_frames(jobs, profile)
        try:
            results, save_errors = self._save_snapshot_frames(frames, profile)
            errors.update(save_errors)
        finally:
            self._cleanup_snapshot_frames(frames)
        return results, errors

    def _capture_runtime_snapshots_now(self, video, audio, profile):
        if not self.snapshot_lock.acquire(blocking=False):
            return {}, {"snapshot": "snapshot capture already running"}, {}
        try:
            jobs = self._runtime_snapshot_jobs(video, audio, profile)
            frames, errors = self._capture_snapshot_frames(jobs, profile)
            results, save_errors = self._save_snapshot_frames(frames, profile)
            errors.update(save_errors)
            return results, errors, frames
        finally:
            self.snapshot_lock.release()

    def _prune_snapshots(self):
        keep = {f"{kind}_snapshot.jpg" for kind in SNAPSHOT_KINDS}
        for item in SNAPSHOT_DIR.glob("*.jpg"):
            if item.name not in keep:
                item.unlink(missing_ok=True)

    def _resolve_snapshot_channel(self, kind, profile, force=True):
        if kind == "audio":
            sources = m3u_sources(profile.get("audio_playlist", ""), profile.get("audio_local_m3u", ""), "本地音频 M3U")
            if not sources:
                raise RuntimeError("no audio M3U configured")
            with self.lock:
                active_audio = self.status.get("active_audio_channel") or profile.get("audio_channel")
            channels = []
            if active_audio:
                channels.append(active_audio)
            for channel in selected_audio_channels(profile):
                if channel not in channels:
                    channels.append(channel)
            last_exc = None
            for channel in channels:
                try:
                    return self.resolver.find_any_sources(sources, channel, force=force)
                except Exception as exc:
                    last_exc = exc
            if last_exc:
                raise last_exc
            raise RuntimeError("audio channel name is empty (audio playlist is set but audio_channel field is blank)")

        with self.lock:
            active = self.status.get("active_channel") or profile.get("video_primary")
        sources = m3u_sources(profile.get("video_playlist", ""), profile.get("video_local_m3u", ""), "本地视频 M3U")
        if not sources:
            raise RuntimeError("no video M3U configured")
        return self.resolver.find_any_sources(sources, active, force=force)

    def capture_snapshots(self):
        profile = self.get_profile()
        if not self.snapshot_lock.acquire(timeout=5):
            raise RuntimeError("snapshot capture already running")
        try:
            with self.lock:
                jobs = list(self.current_snapshot_jobs)
            if not jobs:
                jobs = []
                timeout = coerce_int(profile.get("timeout_seconds"), DEFAULT_PROFILE["timeout_seconds"], minimum=5)
                video_channel = self._resolve_snapshot_channel("video", profile, force=True)
                jobs.append(("cache_video", self._direct_input(video_channel.url, timeout, video_channel.headers), f"{video_channel.name} 原始"))
                jobs.append(("video", self._direct_input(video_channel.url, timeout, video_channel.headers), f"{video_channel.name} 延迟后"))
                try:
                    audio_channel = self._resolve_snapshot_channel("audio", profile, force=True)
                except Exception:
                    audio_channel = None
                if audio_channel and audio_channel.url:
                    jobs.append(("cache_audio", self._direct_input(audio_channel.url, timeout, audio_channel.headers), f"{audio_channel.name} 原始"))
                    jobs.append(("audio", self._direct_input(audio_channel.url, timeout, audio_channel.headers), f"{audio_channel.name} 延迟后"))
            results, errors = self._capture_snapshot_jobs(jobs, profile)
        finally:
            self.snapshot_lock.release()
        if errors and not results:
            raise RuntimeError("; ".join(f"{kind}: {msg}" for kind, msg in errors.items()))
        return {
            "snapshots": [results[kind] for kind in SNAPSHOT_KINDS if kind in results],
            "errors": errors,
        }

    def capture_snapshot(self, kind):
        profile = self.get_profile()
        if not self.snapshot_lock.acquire(timeout=5):
            raise RuntimeError("snapshot capture already running")
        try:
            with self.lock:
                jobs = [job for job in self.current_snapshot_jobs if job[0] == kind]
            if jobs:
                _kind, input_args, source_name = jobs[0]
                timeout = coerce_int(profile.get("timeout_seconds"), DEFAULT_PROFILE["timeout_seconds"], minimum=5)
                frame = self._capture_snapshot_frame(input_args, timeout)
                try:
                    return self._save_snapshot_from_frame(frame, kind, profile, source_name)
                finally:
                    frame.unlink(missing_ok=True)
            channel = self._resolve_snapshot_channel(kind, profile, force=True)
            return self._capture_url_snapshot(kind, channel.url, channel.name, profile, channel.headers)
        finally:
            self.snapshot_lock.release()

    def clear_runtime(self, target):
        if target == "hls":
            self._clear_hls()
            self.log("cleared HLS output")
            return {"ok": True, "target": target}
        if target == "state":
            clear_directory_contents(STATE_DIR, keep={"profile.json", OFFSET_STATE.name, "recordings"})
            SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
            RECORDING_DIR.mkdir(parents=True, exist_ok=True)
            json_save(PROFILE_PATH, _strip_url_fields(self.get_profile()))
            self.log("cleared runtime state")
            return {"ok": True, "target": target}
        raise RuntimeError("target must be hls or state")

    def _recording_status_snapshot(self):
        with self.recording_lock:
            session = self.recording_session
            active = session.as_dict() if session else None
            return {
                "supported": True,
                "running": bool(session and session.status in {"starting", "running", "stopping"}),
                "active": active,
            }

    def _recording_dir(self, session_id):
        return RECORDING_DIR / session_id

    def _recording_meta_path(self, session_id):
        return self._recording_dir(session_id) / "recording.json"

    def _recording_playlist_path(self, session_id):
        return self._recording_dir(session_id) / "recording.m3u8"

    def _recording_output_ext(self, segment_type):
        return ".m4s" if segment_type == "fmp4" else ".ts"

    def _current_live_segment_type(self):
        playlist = self._current_served_hls_playlist()
        try:
            text = playlist.read_text(encoding="utf-8", errors="replace")
            if "#EXT-X-MAP" in text or ".m4s" in text:
                return "fmp4"
        except OSError:
            pass
        if list(HLS_DIR.glob("init_*.mp4")) or list(HLS_DIR.glob("*.m4s")):
            return "fmp4"
        return "mpegts"

    def _recording_allowed_source(self, path):
        return (
            path.name == "index.m3u8"
            or (path.name.startswith("live_") and path.suffix in {".ts", ".m4s"})
            or (path.name.startswith("init_") and path.suffix == ".mp4")
        )

    def _recording_copy_file(self, source, target):
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.tmp")
        shutil.copy2(source, tmp)
        os.replace(tmp, target)

    def _recording_sync_source(self, session, seen):
        session_dir = self._recording_dir(session.session_id)
        copied = 0
        source_playlist = self._current_served_hls_playlist()
        if not source_playlist.exists():
            return 0

        try:
            playlist_stat = source_playlist.stat()
        except FileNotFoundError:
            return 0
        playlist_name = source_playlist.name
        playlist_key = (playlist_name, playlist_stat.st_mtime_ns, playlist_stat.st_size)
        if seen.get(playlist_name) != playlist_key[1:]:
            self._recording_copy_file(source_playlist, self._recording_playlist_path(session.session_id))
            seen[playlist_name] = playlist_key[1:]
            copied += 1

        for source in HLS_DIR.iterdir():
            if not source.is_file() or not self._recording_allowed_source(source) or source.name == playlist_name:
                continue
            try:
                stat = source.stat()
            except FileNotFoundError:
                continue
            key = (stat.st_mtime_ns, stat.st_size)
            if seen.get(source.name) == key:
                continue
            self._recording_copy_file(source, session_dir / source.name)
            seen[source.name] = key
            copied += 1
        return copied

    def _recording_touch_endlist(self, session):
        playlist = self._recording_playlist_path(session.session_id)
        try:
            text = playlist.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        if "#EXT-X-ENDLIST" in text:
            return
        tmp = playlist.with_name(f".{playlist.name}.tmp")
        tmp.write_text(text.rstrip() + "\n#EXT-X-ENDLIST\n", encoding="utf-8")
        os.replace(tmp, playlist)

    def _recording_segment_count(self, playlist_path, segment_ext):
        try:
            lines = playlist_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return 0
        count = 0
        for line in lines:
            raw = line.strip()
            if not raw or raw.startswith("#"):
                continue
            if Path(urlsplit(raw).path or raw).suffix == segment_ext:
                count += 1
        return count

    def _save_recording_meta(self, session):
        session_dir = self._recording_dir(session.session_id)
        session_dir.mkdir(parents=True, exist_ok=True)
        tmp = self._recording_meta_path(session.session_id).with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(session.as_dict(), f, ensure_ascii=True, indent=2)
            f.write("\n")
        os.replace(tmp, self._recording_meta_path(session.session_id))

    def _load_recording_meta(self, session_id):
        with self._recording_meta_path(session_id).open("r", encoding="utf-8") as f:
            return json.load(f)

    def list_recordings(self):
        RECORDING_DIR.mkdir(parents=True, exist_ok=True)
        items = []
        for session_dir in sorted([p for p in RECORDING_DIR.iterdir() if p.is_dir()], key=lambda p: p.stat().st_mtime, reverse=True):
            meta_path = session_dir / "recording.json"
            if not meta_path.exists():
                continue
            try:
                with meta_path.open("r", encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue
            data["session_id"] = session_dir.name
            data["playlist_url"] = f"/recordings/{session_dir.name}/recording.m3u8"
            merged_name = data.get("merged_path") or ""
            data["merged_url"] = f"/recordings/{session_dir.name}/{Path(merged_name).name}" if merged_name else ""
            data["file_count"] = sum(1 for item in session_dir.iterdir() if item.is_file())
            items.append(data)
        return {"recordings": items}

    def start_recording(self, payload=None):
        profile = self.get_profile()
        payload = payload or {}
        if not bool(self.status.get("running")):
            raise RuntimeError("live stream is not running")
        playlist_source = self._current_served_hls_playlist()
        if not playlist_source.exists():
            raise RuntimeError("HLS playlist is not ready")
        session_id = str(payload.get("session_id") or f"rec_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}")
        label = str(payload.get("label") or self.status.get("active_channel") or profile.get("channel_name") or "recording").strip()
        with self.recording_lock:
            if self.recording_session and self.recording_session.status in {"starting", "running", "stopping"}:
                raise RuntimeError("recording already running")
            session_dir = self._recording_dir(session_id)
            if session_dir.exists():
                shutil.rmtree(session_dir, ignore_errors=True)
            session_dir.mkdir(parents=True, exist_ok=True)
            session = RecordingSession(
                session_id=session_id,
                label=label,
                status="starting",
                started_at=now(),
                started_at_unix=time.time(),
                source_playlist=f"/{playlist_source.name}",
                source_segment_type=self._current_live_segment_type(),
                segment_time=effective_segment_time(profile),
                playlist_path=str(self._recording_playlist_path(session_id)),
                merge_status="not_requested",
            )
            self.recording_session = session
            self.recording_stop_event.clear()
            self._save_recording_meta(session)
            self.recording_thread = threading.Thread(target=self._recording_worker, args=(session,), daemon=True)
            self.recording_thread.start()
        self.log(f"recording started: {session_id}")
        return session.as_dict()

    def stop_recording(self):
        with self.recording_lock:
            session = self.recording_session
            if not session:
                return {"ok": True, "recording": None}
            if session.status in {"stopped", "error"}:
                return {"ok": True, "recording": session.as_dict()}
            session.status = "stopping"
            self._save_recording_meta(session)
            self.recording_stop_event.set()
            thread = self.recording_thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=20)
        with self.recording_lock:
            session = self.recording_session
            return {"ok": True, "recording": session.as_dict() if session else None}

    def merge_recording(self, session_id, output_format="mkv"):
        session_id = str(session_id or "").strip()
        if not session_id:
            raise RuntimeError("missing session_id")
        meta = self._load_recording_meta(session_id)
        if meta.get("status") in {"starting", "running", "stopping"}:
            raise RuntimeError("recording is still running")
        output_format = str(output_format or "mkv").strip().lower()
        if output_format not in {"mkv", "mp4"}:
            raise RuntimeError("output_format must be mkv or mp4")
        session_dir = self._recording_dir(session_id)
        playlist_path = self._recording_playlist_path(session_id)
        if not playlist_path.exists():
            raise RuntimeError("recording playlist is missing")
        output_path = session_dir / f"{session_id}.{output_format}"
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
            "-allowed_extensions", "ALL",
            "-protocol_whitelist", "file,crypto",
            "-i", playlist_path.name,
            "-c", "copy",
            str(output_path),
        ]
        self.log(f"recording merge started: {session_id} -> {output_path.name}")
        proc = subprocess.run(cmd, cwd=str(session_dir), capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or "recording merge failed")
        meta["merged_path"] = str(output_path)
        meta["merge_status"] = "done"
        meta["merge_message"] = f"merged to {output_path.name}"
        merged_session = RecordingSession(**{k: meta.get(k) for k in RecordingSession.__dataclass_fields__.keys()})
        with self.recording_lock:
            if self.recording_session and self.recording_session.session_id == session_id:
                self.recording_session = merged_session
        self._save_recording_meta(merged_session)
        return {"ok": True, "recording": merged_session.as_dict()}

    def _recording_worker(self, session):
        session_dir = self._recording_dir(session.session_id)
        playlist_path = Path(session.playlist_path)
        try:
            seen = {}
            with self.lock:
                timeout_seconds = coerce_int(self.profile.get("timeout_seconds"), DEFAULT_PROFILE["timeout_seconds"], minimum=5)
            ready_deadline = time.time() + max(30, timeout_seconds + 20)
            self.log(f"recording mirror started: {session.session_id}")
            while not self.recording_stop_event.is_set():
                copied = self._recording_sync_source(session, seen)
                if playlist_path.exists():
                    session.segment_count = self._recording_segment_count(
                        playlist_path,
                        self._recording_output_ext(session.source_segment_type),
                    )
                    session.last_update_at = now()
                    session.last_update_unix = time.time()
                    if session.status == "starting":
                        session.status = "running"
                    if copied or session.segment_count:
                        self._save_recording_meta(session)
                elif time.time() > ready_deadline:
                    session.status = "error"
                    session.error = "recording playlist was not created"
                    self._save_recording_meta(session)
                    self.recording_stop_event.set()
                    break
                time.sleep(1)

            self._recording_sync_source(session, seen)
            self._recording_touch_endlist(session)
            session.segment_count = self._recording_segment_count(
                playlist_path,
                self._recording_output_ext(session.source_segment_type),
            )
            session.last_update_at = now()
            session.last_update_unix = time.time()
            session.stopped_at = now()
            session.stopped_at_unix = time.time()
            session.pid = None
            if session.status != "error":
                session.status = "stopped"
                if not session.merge_status:
                    session.merge_status = "not_requested"
            self._save_recording_meta(session)
        except Exception as exc:
            session.status = "error"
            session.error = str(exc)
            session.stopped_at = now()
            session.stopped_at_unix = time.time()
            self._save_recording_meta(session)
            self.log(f"recording error: {exc}")
        finally:
            with self.recording_lock:
                self.recording_session = session
                self.recording_thread = None
                self._save_recording_meta(session)


MANAGER = LiveManager()


class Handler(SimpleHTTPRequestHandler):
    server_version = "LiveSyncWebUI/1.0"

    def end_headers(self):
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/" or path == "/index.html":
            return self.send_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
        if path.startswith("/static/"):
            try:
                return self.send_file(safe_child_path(STATIC_DIR, path.removeprefix("/static/")))
            except PermissionError:
                return self.send_error(HTTPStatus.NOT_FOUND)
        if path == "/api/status":
            return self.send_json(MANAGER.get_status())
        if path == "/api/profile":
            return self.send_json(MANAGER.get_public_profile())
        if path == "/api/logs":
            return self.send_json({"lines": list(MANAGER.logs)})
        if path == "/api/timer-rois":
            return self.send_json({"entries": MANAGER._timer_roi_entries()})
        if path == "/api/roi":
            return self.send_json(MANAGER.roi)
        if path == "/api/recordings":
            return self.send_json(MANAGER.list_recordings())
        if path == "/api/snapshots":
            SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
            shots = []
            for kind in SNAPSHOT_KINDS:
                p = SNAPSHOT_DIR / f"{kind}_snapshot.jpg"
                if p.exists():
                    shots.append({"kind": kind, "url": f"/snapshots/{p.name}", "name": p.name, "mtime": p.stat().st_mtime})
                else:
                    shots.append({"kind": kind, "url": "", "name": f"{kind}_snapshot.jpg", "mtime": 0})
            return self.send_json({"snapshots": shots})
        if path == "/emby.m3u":
            return self.send_text(self.emby_m3u(), "audio/x-mpegurl; charset=utf-8")
        if path == "/guide.xml":
            return self.send_text(self.guide_xml(), "application/xml; charset=utf-8")
        if path == "/cctv5.strm":
            return self.send_text(self.public_url("/index.m3u8") + "\n", "text/plain; charset=utf-8")
        if path.startswith("/recordings/"):
            try:
                return self.send_file(safe_child_path(RECORDING_DIR, path.removeprefix("/recordings/")))
            except PermissionError:
                return self.send_error(HTTPStatus.NOT_FOUND)
        if path == "/index.m3u8" or path.endswith(".ts") or path.endswith(".m4s") or re.match(r"^/init_[A-Za-z0-9_.-]+\.mp4$", path):
            if path == "/index.m3u8":
                target = MANAGER._current_served_hls_playlist()
            else:
                try:
                    target = safe_child_path(HLS_DIR, path.lstrip("/"))
                except PermissionError:
                    return self.send_error(HTTPStatus.NOT_FOUND)
            if not target.exists() and path == "/index.m3u8":
                return self.send_text("HLS playlist is not ready\n", "text/plain; charset=utf-8", status=HTTPStatus.SERVICE_UNAVAILABLE, extra_headers={"Retry-After": "2"})
            return self.send_file(target)
        if path.startswith("/roi-previews/"):
            try:
                return self.send_file(safe_child_path(ROI_PREVIEW_DIR, path.removeprefix("/roi-previews/")))
            except PermissionError:
                return self.send_error(HTTPStatus.NOT_FOUND)
        if path.startswith("/snapshots/"):
            try:
                return self.send_file(safe_child_path(SNAPSHOT_DIR, path.removeprefix("/snapshots/")))
            except PermissionError:
                return self.send_error(HTTPStatus.NOT_FOUND)
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self):
        path = urlsplit(self.path).path
        try:
            data = self.read_json()
            if path == "/api/profile":
                return self.send_json(MANAGER.set_profile(data))
            if path == "/api/playlists/preview":
                return self.send_json(MANAGER.resolver.search_any_sources(
                    m3u_sources(data.get("url", ""), data.get("text", ""), data.get("label", "本地 M3U")),
                    data.get("query", ""),
                    force=bool(data.get("force")),
                ))
            if path == "/api/start":
                if data:
                    MANAGER.set_profile(data)
                MANAGER.start()
                return self.send_json(MANAGER.get_status())
            if path == "/api/restart":
                MANAGER.restart(data if data else None)
                return self.send_json(MANAGER.get_status())
            if path == "/api/stop":
                MANAGER.stop(source="manual")
                return self.send_json(MANAGER.get_status())
            if path == "/api/snapshot":
                return self.send_json(MANAGER.capture_snapshot(data.get("kind", "video")))
            if path == "/api/snapshots/capture":
                return self.send_json(MANAGER.capture_snapshots())
            if path == "/api/schedule/refresh":
                return self.send_json(MANAGER.refresh_schedule(force=True))
            if path == "/api/ocr/test":
                profile = MANAGER.get_profile()
                if data:
                    profile.update(data)
                return self.send_json(MANAGER._test_ocr_provider(profile))
            if path == "/api/recording/start":
                return self.send_json(MANAGER.start_recording(data))
            if path == "/api/recording/stop":
                return self.send_json(MANAGER.stop_recording())
            if path == "/api/recording/merge":
                return self.send_json(MANAGER.merge_recording(data.get("session_id", ""), data.get("output_format", "mkv")))
            if path == "/api/clear":
                return self.send_json(MANAGER.clear_runtime(data.get("target", "")))
            if path == "/api/timer-rois/delete":
                return self.send_json(MANAGER.delete_timer_roi(data.get("key", ""), roi=data.get("roi", True), preview=data.get("preview", True)))
            if path == "/api/timer-rois/delete-preview":
                return self.send_json(MANAGER.delete_timer_roi(data.get("key", ""), roi=False, preview=True))
            if path == "/api/roi":
                MANAGER.roi = data
                json_save(ROI_PATH, data)
                return self.send_json(data)
        except Exception as exc:
            MANAGER.log(f"api error {path}: {exc}")
            return self.send_json({"error": str(exc)}, status=500)
        self.send_error(HTTPStatus.NOT_FOUND)

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_text(self, text, content_type, status=200, extra_headers=None):
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path, content_type=None):
        path = Path(path)
        if not path.exists() or not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        file_size = path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", content_type or mimetypes.guess_type(str(path))[0] or "application/octet-stream")
        self.send_header("Content-Length", str(file_size))
        self.end_headers()
        with path.open("rb") as f:
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                shutil.copyfileobj(f, self.wfile, length=1024 * 1024)

    def public_url(self, path):
        base = MANAGER.get_profile().get("public_base_url", "").rstrip("/")
        if base:
            return f"{base}{path}"
        host = self.headers.get("Host") or f"127.0.0.1:{PORT}"
        return f"http://{host}{path}"

    def emby_m3u(self):
        p = MANAGER.get_profile()
        return "\n".join([
            "#EXTM3U",
            f'#EXTINF:-1 tvg-id="{p.get("channel_id")}" tvg-name="{p.get("channel_name")}" tvg-chno="{p.get("channel_number")}" tvg-group="{p.get("channel_group")}",{p.get("channel_name")}',
            self.public_url("/index.m3u8"),
            "",
        ])

    def guide_xml(self):
        p = MANAGER.get_profile()
        t = int(time.time())
        start = time.strftime("%Y%m%d%H%M%S +0000", time.gmtime(t - 3600))
        stop = time.strftime("%Y%m%d%H%M%S +0000", time.gmtime(t + 86400))
        return "\n".join([
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<tv generator-info-name="live-sync-webui">',
            f'  <channel id="{p.get("channel_id")}">',
            f'    <display-name>{p.get("channel_name")}</display-name>',
            "  </channel>",
            f'  <programme start="{start}" stop="{stop}" channel="{p.get("channel_id")}">',
            "    <title>Live</title>",
            "    <category>Sports</category>",
            "  </programme>",
            "</tv>",
            "",
        ])


class ReusableServer(ThreadingHTTPServer):
    allow_reuse_address = True


def main():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    HLS_DIR.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    RECORDING_DIR.mkdir(parents=True, exist_ok=True)
    mimetypes.add_type("application/vnd.apple.mpegurl", ".m3u8")
    mimetypes.add_type("audio/x-mpegurl", ".m3u")
    mimetypes.add_type("video/mp2t", ".ts")
    mimetypes.add_type("video/mp4", ".m4s")
    mimetypes.add_type("video/mp4", ".mp4")
    MANAGER.warm_channel_cache()
    MANAGER.start_scheduler()
    if env("AUTO_START", "0").lower() not in ("0", "false", "no"):
        MANAGER.start(source="manual")
    with ReusableServer(("0.0.0.0", PORT), Handler) as httpd:
        print(f"Live Sync Web UI listening on http://0.0.0.0:{PORT}", flush=True)
        try:
            httpd.serve_forever()
        finally:
            MANAGER.stop_scheduler()
            MANAGER.stop()


if __name__ == "__main__":
    main()
