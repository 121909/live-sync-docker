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
from rapidocr_onnxruntime import RapidOCR

from .auto_align import (
    ALIGN_MAX_CAPTURE_SKEW_SECONDS,
    ALIGN_MAX_FINISH_DELTA_SECONDS,
    ALIGN_RETRY_BACKOFF_BASE_SECONDS,
    ALIGN_RETRY_BACKOFF_MAX_SECONDS,
    ALIGN_STATE_CAPTURE_FAILED,
    ALIGN_STATE_DISABLED,
    ALIGN_STATE_ALIGNED,
    ALIGN_STATE_PROBING,
    ALIGN_STATE_VERIFYING,
    ALIGN_STATE_WAITING,
    AlignmentMonitor,
    AutoAlignController,
)


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
SNAPSHOT_DIR = STATE_DIR / "snapshots"
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
HLS_EXTINF_RE = re.compile(r"^#EXTINF:([0-9.]+)")
RAPIDOCR_CONFIG_PATH = APP_ROOT / "configs" / "ocr" / "rapidocr_timer.yaml"
RAPIDOCR_PRESET_ROIS = (
    (0.02, 0.02, 0.42, 0.36),
    (0.02, 0.02, 0.40, 0.50),
    (0.60, 0.02, 0.38, 0.44),
    (0.60, 0.04, 0.36, 0.52),
)
TIMER_TEXT_PATTERNS = [
    re.compile(r"(?<!\d)(\d{1,3}:\d{2}\+\d{1,2}:\d{2})(?!\d)"),
    re.compile(r"(?<!\d)(\d{1,3}:\d{2}\+\d{1,2})(?!\d)"),
    re.compile(r"(?<!\d)(\d{1,3}:\d{2})(?!\d)"),
]
HLS_RECOVERY_WINDOW_SECONDS = int(os.environ.get("HLS_RECOVERY_WINDOW_SECONDS", "300") or 300)
HLS_CLIENT_GRACE_SECONDS = int(os.environ.get("HLS_CLIENT_GRACE_SECONDS", "240") or 240)
FFMPEG_FAST_TIMEOUT_CAP_SECONDS = 10
ALIGN_CAPTURE_ANCHOR_MARGIN_SECONDS = 6.0


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
        "auto_align_threshold": float(env("AUTO_ALIGN_THRESHOLD", "1") or 1),
        "auto_align_max_offset": float(env("AUTO_ALIGN_MAX_OFFSET", "180") or 180),
        "auto_align_debug_override": env_bool("AUTO_ALIGN_DEBUG_OVERRIDE", False),
        "snapshot_interval": auto_align_interval,
        "schedule_enabled": env_bool("SCHEDULE_ENABLED", True),
        "schedule_recording_enabled": env_bool("SCHEDULE_RECORDING_ENABLED", False),
        "schedule_selected_event_ids": env_list("SCHEDULE_SELECTED_EVENT_IDS"),
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
    return "mpegts"


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


def normalize_ocr_provider(value):
    provider = coerce_text(value).strip().lower()
    return provider if provider in OCR_PROVIDERS else ""


def ocr_provider_ready(profile):
    return rapidocr_available() or any(ocr_provider_ready_for(provider, profile) for provider in ocr_provider_order(profile))


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


def rapidocr_available():
    return RAPIDOCR_CONFIG_PATH.exists()


DEFAULT_PROFILE = make_default_profile()
DEFAULT_PROFILE["auto_align_enabled"] = ocr_provider_ready(DEFAULT_PROFILE)

URL_SAVE_KEYS = {
    "video_url",
    "audio_url",
    "public_base_url",
}
RUNTIME_AUTO_ALIGN_KEYS = {
    "auto_align_interval",
    "auto_align_threshold",
    "auto_align_max_offset",
    "auto_align_debug_override",
    "schedule_enabled",
    "schedule_recording_enabled",
    "schedule_selected_event_ids",
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


LOCAL_CACHE_MAX_SECONDS = 600


def effective_local_cache_seconds(profile):
    segment = effective_segment_time(profile)
    configured = coerce_int(profile.get("local_cache_seconds"), DEFAULT_PROFILE["local_cache_seconds"], minimum=30)
    offset = abs(coerce_float(profile.get("offset_seconds"), DEFAULT_PROFILE["offset_seconds"]))
    # Honor the configured cache depth so upstream jitter/short stalls are
    # absorbed locally instead of starving the mux. Keep at least what the
    # handoff needs (offset + a few segments), and cap at an absolute ceiling
    # so a long session cannot accumulate an unbounded source cache.
    needed = max(configured, offset + segment * 3)
    return int(math.ceil(min(needed, LOCAL_CACHE_MAX_SECONDS)))


def local_cache_list_size(profile):
    return max(8, int(math.ceil(effective_local_cache_seconds(profile) / effective_segment_time(profile))) + 4)


def _strip_url_fields(profile):
    return {k: v for k, v in profile.items() if k not in URL_SAVE_KEYS}

def _redact_url(text):
    if not env_bool("LOG_REDACT_URLS", False):
        return str(text or "")
    return re.sub(r"https?://[^\s\"'<>)]+", "<URL>", str(text or ""))


def _subprocess_error_summary(exc, *, max_lines=3):
    detail = str(exc).strip()
    stderr = ""
    stdout = ""
    if isinstance(exc, subprocess.CalledProcessError):
        stderr = str(exc.stderr or "").strip()
        stdout = str(exc.stdout or "").strip()
        detail = f"exit code {exc.returncode}"
    elif isinstance(exc, subprocess.TimeoutExpired):
        stderr = str(exc.stderr or "").strip()
        stdout = str(exc.stdout or "").strip()
        timeout = exc.timeout
        detail = f"timed out after {timeout}s" if timeout is not None else "timed out"
    lines = []
    for text in (stderr, stdout):
        if not text:
            continue
        lines.extend(part.strip() for part in text.splitlines() if part.strip())
    if lines:
        tail = " || ".join(_redact_url(line) for line in lines[-max_lines:])
        return f"{detail} | probe output: {tail}"
    return _redact_url(detail)


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


PACKET_CORRUPT_RE = re.compile(r"Packet corrupt \(stream = ([0-9]+), dts = ([0-9-]+)\), dropping it\.", re.IGNORECASE)


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
    provider: str = ""
    note: str = ""


@dataclass(frozen=True)
class OcrProviderResult:
    value: object | None = None
    request_failed: bool = False


def coerce_clock_sample(found, media_time=0.0):
    if not found:
        return None
    if isinstance(found, ClockSample):
        if found.media_time == media_time:
            return found
        return ClockSample(media_time, found.game_time, found.text, found.roi, found.provider, found.note)
    if isinstance(found, (list, tuple)):
        roi = found[2] if len(found) > 2 else None
        provider = found[3] if len(found) > 3 else ""
        note = found[4] if len(found) > 4 else ""
        return ClockSample(media_time, found[0], found[1], roi, provider, note)
    return None

@dataclass(frozen=True)
class FrameCaptureResult:
    path: Path
    kind: str
    started_at: float
    finished_at: float
    media_time: float = 0.0
    source: str = ""
    anchor_time: float = 0.0
    seconds_back: float = 0.0


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
class PlaylistProgramWindow:
    start_time: float
    end_time: float
    segment_count: int


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
        self.thread = None
        self.stop_event = threading.Event()
        self.processes = []
        self.process_tails = {}
        self.current_snapshot_jobs = []
        self.snapshot_lock = threading.Lock()
        self.snapshot_file_lock = threading.Lock()
        self.ocr_lock = threading.Lock()
        self.rapidocr_engine = None
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
        self._auto_align_source_cache = None
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
            "last_ocr_diagnostic": {},
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
        merged["auto_align_threshold"] = coerce_float(merged.get("auto_align_threshold"), DEFAULT_PROFILE["auto_align_threshold"], minimum=0.1)
        merged["auto_align_max_offset"] = coerce_float(merged.get("auto_align_max_offset"), DEFAULT_PROFILE["auto_align_max_offset"], minimum=1)
        merged["auto_align_debug_override"] = parse_bool(merged.get("auto_align_debug_override", DEFAULT_PROFILE["auto_align_debug_override"]))
        merged["snapshot_interval"] = merged["auto_align_interval"]
        merged["schedule_enabled"] = parse_bool(merged.get("schedule_enabled", DEFAULT_PROFILE["schedule_enabled"]))
        merged["schedule_recording_enabled"] = parse_bool(merged.get("schedule_recording_enabled", DEFAULT_PROFILE.get("schedule_recording_enabled", False)))
        merged["schedule_selected_event_ids"] = [
            str(x).strip() for x in (merged.get("schedule_selected_event_ids", []) or []) if str(x).strip()
        ]
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
                "threshold": coerce_float(aa.get("auto_align_threshold"), DEFAULT_PROFILE["auto_align_threshold"]),
                "max_offset": coerce_float(aa.get("auto_align_max_offset"), DEFAULT_PROFILE["auto_align_max_offset"]),
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
            self._auto_align_source_cache = None
        self.log(f"live pipeline stopped ({source})")

    def _clear_stream_pipeline(self):
        self._clear_hls()
        shutil.rmtree(WORK_DIR, ignore_errors=True)
        WORK_DIR.mkdir(parents=True, exist_ok=True)
        clear_directory_contents(SNAPSHOT_DIR)
        with self.lock:
            self.current_snapshot_jobs = []
            self._auto_align_source_cache = None
            self.status["last_segment_at"] = None
            self.status["last_snapshot_at"] = None
            self.status["last_ocr_results"] = {kind: None for kind in SNAPSHOT_KINDS}
            self.status["auto_align_state"] = "idle"
            self.status["auto_align_monitor"] = {}
        self.log("cleared stream pipeline runtime")

    def restart(self, profile=None, source="manual", clean=False):
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
                if clean:
                    self.log(f"clean restart queued ({source}): waiting for previous pipeline to stop")
                else:
                    self.log(f"restart queued ({source}): waiting for previous pipeline to stop")
                threading.Thread(
                    target=self._restart_when_stopped,
                    args=(request_id, source, clean),
                    name="live-sync-restart-waiter",
                    daemon=True,
                ).start()
                return
            if self.thread and not self.thread.is_alive():
                self.thread = None
        if clean:
            self._clear_stream_pipeline()
        self.start(source=source)

    def _restart_when_stopped(self, request_id, source, clean=False):
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
        if clean:
            self._clear_stream_pipeline()
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

    def _format_ffmpeg_stderr_line(self, line):
        text = _redact_url(line)
        match = PACKET_CORRUPT_RE.search(text)
        if match:
            stream_idx, dts = match.groups()
            return f"上游 TS 包损坏，已丢弃后继续 (stream={stream_idx}, dts={dts})"
        return text

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
                self.log(f"{name}: {self._format_ffmpeg_stderr_line(line)}")
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

    def _is_http_input(self, url):
        return str(url or "").lower().startswith(("http://", "https://"))

    def _http_input_options(self, url, headers=None):
        if not str(url or "").lower().startswith(("http://", "https://")):
            return []
        headers = {**default_request_headers(), **dict(headers or {})}
        user_agent = headers.pop("User-Agent", "") or ffmpeg_user_agent()
        args = [
            "-user_agent", user_agent,
        ]
        if urlsplit(str(url)).path.lower().endswith(".m3u8"):
            args += ["-http_persistent", "0"]
        if headers:
            header_text = "".join(f"{key}: {value}\r\n" for key, value in headers.items())
            args += ["-headers", header_text]
        return args

    def _remote_input_args(self, url, timeout, headers=None, *, timeout_cap=None):
        timeout = coerce_int(timeout, DEFAULT_PROFILE["timeout_seconds"], minimum=5)
        if timeout_cap is not None:
            timeout = min(timeout, int(timeout_cap))
        return [
            "-rw_timeout", str(timeout * 1_000_000),
            *self._http_input_options(url, headers),
            "-i", url,
        ]

    def _local_input_args(self, url):
        return ["-i", url]

    def _stable_input_args(self, url, timeout, headers=None):
        if self._is_http_input(url):
            return self._remote_input_args(url, timeout, headers)
        return self._local_input_args(url)

    def _fast_input_args(self, url, timeout, headers=None):
        if self._is_http_input(url):
            return self._remote_input_args(url, timeout, headers, timeout_cap=FFMPEG_FAST_TIMEOUT_CAP_SECONDS)
        return self._local_input_args(url)

    def _probe_video_codec(self, channel, timeout):
        if not channel or not channel.url:
            return ""
        cmd = [
            "ffprobe", "-hide_banner", "-loglevel", "error",
            "-rw_timeout", str(timeout * 1_000_000),
            *self._http_input_options(channel.url, channel.headers),
            "-select_streams", "v:0",
            "-show_entries", "stream=codec_name",
            "-of", "default=nokey=1:noprint_wrappers=1",
            channel.url,
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 5, check=True)
        except Exception as exc:
            summary = _subprocess_error_summary(exc)
            self.log(f"video codec probe failed for {channel.name}: {summary}; using generic copy path")
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
            "-select_streams", "a",
            "-show_entries", "stream=index,codec_name",
            "-of", "json",
            channel.url,
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 5, check=True)
        except Exception as exc:
            summary = _subprocess_error_summary(exc)
            self.log(f"audio stream probe failed for {channel.name}: {summary}; using first audio stream")
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
        video_codec = self._probe_video_codec(channel, coerce_int(profile.get("timeout_seconds"), DEFAULT_PROFILE["timeout_seconds"], minimum=5))
        header_label = f", headers={','.join(channel.headers.keys())}" if channel.headers else ""
        context = (
            f"source={kind} url={channel.url}{header_label}; "
            f"cache={playlist.name}; seconds={effective_local_cache_seconds(profile)}; copy"
        )
        return self._start_process([
            "ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
            *self._stable_input_args(channel.url, profile.get("timeout_seconds"), channel.headers),
            *map_args,
            *self._video_copy_args(video_codec),
            "-c:a", "copy",
            "-f", "hls", "-hls_time", segment, "-hls_list_size", str(list_size),
            "-hls_delete_threshold", "2",
            "-hls_flags", "delete_segments+omit_endlist+append_list+program_date_time",
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
                if cached_audio.url and not self._input_has_video_stream(cached_audio.url, coerce_int(profile.get("timeout_seconds"), DEFAULT_PROFILE["timeout_seconds"], minimum=5)):
                    self.log(f"audio cache has no video stream: {Path(cached_audio.url).name}; audio-side OCR/auto-align unavailable")
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
            *self._stable_input_args(url, timeout, channel.headers),
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

    def _direct_input(self, url, timeout, headers=None):
        return self._stable_input_args(url, timeout, headers)

    def _snapshot_input(self, url, timeout, headers=None):
        return self._fast_input_args(url, timeout, headers)

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
        channel_prefers_fmp4 = False
        configured_segment_type = hls_segment_type(profile)
        compatible_mux = configured_segment_type == "auto"
        mux_segment_type = configured_segment_type if configured_segment_type != "auto" else "mpegts"
        if video_codec:
            mux_mode = "auto mpegts" if compatible_mux else mux_segment_type
            self.log(f"video codec detected: {video_codec}; mux mode: {mux_mode}")
        same_source = (
            bool(audio_url)
            and video_url == audio_url
            and dict(video.headers) == dict(audio.headers)
        )
        single_input_av = same_source and not source_cache and abs(offset) < 0.5
        video_input = self._direct_input(video_url, timeout, video.headers)
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
            audio_input = self._direct_input(audio_url, timeout, audio.headers)
            audio_map = f"1:a:{audio_stream_index}"
            audio_input_label = f"input1={source_kind} audio ({audio_url}{audio_header_label})"
        else:
            audio_input_label = "input1=none"
        video_snapshot_input = list(video_input)
        audio_snapshot_input = list(video_input if single_input_av else audio_input)
        snapshot_jobs = []
        audio_copy_bsf = ""

        try:
            if 0.5 <= offset < segment_seconds:
                video_input = self._offset_input_args(video_input, offset)
                video_input_label = f"input0={source_kind} video ({video_url}{video_header_label}, offset +{offset:.3f}s via itsoffset)"
                video_snapshot_input = list(video_input)
            elif offset >= segment_seconds:
                delay_playlist = run_dir / "video_delay.m3u8"
                list_size = max(20, int(offset / segment_seconds) + 20)
                recorder = self._start_delay_recorder(video, delay_playlist, segment, list_size, timeout, "video", video_codec)
                delay_procs.append(recorder)
                self._buffer_delay_input(recorder, delay_playlist, offset, timeout, "buffering video")
                video_input = ["-i", str(delay_playlist)]
                video_input_label = f"input0=local video delay HLS ({delay_playlist.name}, source={video_url}{video_header_label}, offset +{offset:.3f}s)"
                video_snapshot_input = list(video_input)
            elif -segment_seconds < offset <= -0.5:
                if not audio_url:
                    raise RuntimeError("negative offset requires an audio source")
                audio_input = self._offset_input_args(audio_input, abs(offset))
                audio_input_label = f"input1={source_kind} audio ({audio_url}{audio_header_label}, offset {offset:.3f}s via itsoffset)"
                audio_snapshot_input = list(audio_input)
            elif offset <= -segment_seconds:
                if not audio_url:
                    raise RuntimeError("negative offset requires an audio source")
                delay_playlist = run_dir / "audio_delay.m3u8"
                list_size = max(20, int(abs(offset) / segment_seconds) + 20)
                recorder = self._start_delay_recorder(audio, delay_playlist, segment, list_size, timeout, "audio", audio_stream_index=audio_stream_index)
                delay_procs.append(recorder)
                self._buffer_delay_input(recorder, delay_playlist, offset, timeout, "buffering audio")
                audio_input = ["-i", str(delay_playlist)]
                audio_map = "1:a:0"
                audio_input_label = f"input1=local audio delay HLS ({delay_playlist.name}, source={audio_url}{audio_header_label}, offset {offset:.3f}s)"
                audio_snapshot_input = list(audio_input)
        except Exception:
            self._stop_processes(delay_procs)
            shutil.rmtree(run_dir, ignore_errors=True)
            raise

        if source_cache:
            snapshot_jobs.append(("cache_video", list(self._snapshot_input(source_cache.video.url, timeout, video.headers)), f"{video.name} 缓存前"))
            if source_cache.audio and source_cache.audio.url:
                snapshot_jobs.append(("cache_audio", list(self._snapshot_input(source_cache.audio.url, timeout, audio.headers)), f"{audio.name} 缓存前"))
        else:
            snapshot_jobs.append(("cache_video", list(self._snapshot_input(video_url, timeout, video.headers)), f"{video.name} 原始"))
            if audio_url:
                cache_audio_input = list(video_input if single_input_av else self._snapshot_input(audio_url, timeout, audio.headers))
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
            "-avoid_negative_ts", "make_zero",
            "-max_muxing_queue_size", "1024",
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

    def auto_align_publish_status(self, monitor, msg=None):
        self._set_align_monitor_status(monitor, msg)

    def auto_align_refresh_profile(self, profile):
        return self._refresh_runtime_auto_align_profile(profile)

    def auto_align_log(self, message):
        self.log(message)

    def auto_align_read_probe_clocks(self, video_frame, audio_frame, profile):
        video_sample = self._read_top_quarter_clock(video_frame)
        audio_sample = self._read_top_quarter_clock(audio_frame)
        self._save_alignment_pair_snapshots(video_frame, audio_frame, profile, video_sample, audio_sample, stage="candidate")
        return video_sample, audio_sample

    def auto_align_collect_probe_readings(self, video, audio, profile, sample_count, timeout, spacing_seconds, *, stage):
        readings = []
        last_error = ""
        video_text = str(getattr(video, "url", "") or "").strip()
        audio_text = str(getattr(audio, "url", "") or "").strip()
        if not video_text or not audio_text:
            return [], "source cache unavailable: missing local cache input"
        if video_text.startswith(("http://", "https://")) or audio_text.startswith(("http://", "https://")):
            return [], "source cache unavailable: auto-align requires local cache inputs"
        if not video_text.endswith(".m3u8") or not audio_text.endswith(".m3u8"):
            return [], "source cache unavailable: auto-align requires local cache playlists"
        # Candidate sampling must read directly from the pipeline cache inputs so
        # OCR measures the upstream delta itself. Only verify probes should
        # re-sample with a proposed offset applied.
        applied_offset = 0.0 if stage == "candidate" else float(profile.get("offset_seconds", 0) or 0)
        video_offset = max(0.0, applied_offset)
        audio_offset = max(0.0, -applied_offset)
        with tempfile.TemporaryDirectory(prefix="align_probe_") as tmp:
            tmpdir = Path(tmp)
            for idx in range(max(1, int(sample_count or 1))):
                if idx > 0:
                    time.sleep(max(0.0, float(spacing_seconds or 0.0)))
                try:
                    video_cap, audio_cap = self._capture_frame_pair(
                        video,
                        audio,
                        tmpdir / f"sample_{idx:02d}",
                        timeout,
                        video_offset=video_offset,
                        audio_offset=audio_offset,
                    )
                except Exception as exc:
                    last_error = str(exc).strip()
                    continue
                if (
                    self._pair_capture_skew(video_cap, audio_cap) > ALIGN_MAX_CAPTURE_SKEW_SECONDS
                    or self._pair_capture_finish_delta(video_cap, audio_cap) > ALIGN_MAX_FINISH_DELTA_SECONDS
                ):
                    last_error = "capture skew too large: video/audio frame timestamps differ too much"
                    continue
                with self.lock:
                    monitor = self.status.get("auto_align_monitor")
                    if isinstance(monitor, dict):
                        if stage == "verify":
                            monitor["verify_video_seconds_back"] = float(video_cap.seconds_back or 0.0)
                            monitor["verify_audio_seconds_back"] = float(audio_cap.seconds_back or 0.0)
                        else:
                            monitor["candidate_video_seconds_back"] = float(video_cap.seconds_back or 0.0)
                            monitor["candidate_audio_seconds_back"] = float(audio_cap.seconds_back or 0.0)
                video_sample = self._read_top_quarter_clock(video_cap.path)
                audio_sample = self._read_top_quarter_clock(audio_cap.path)
                self._save_alignment_pair_snapshots(video_cap.path, audio_cap.path, profile, video_sample, audio_sample, stage=stage)
                readings.append((video_sample, audio_sample))
        if readings:
            return readings, ""
        return [], last_error or "probe capture failed"

    def auto_align_pair_capture_skew(self, video_cap, audio_cap):
        return self._pair_capture_skew(video_cap, audio_cap)

    def auto_align_verify_candidate(self, video, audio, profile, monitor, candidate):
        return self._verify_alignment_candidate(video, audio, profile, monitor, candidate)

    def auto_align_handoff_candidate(self, video, audio, next_profile, current_pipeline, mux, run_label):
        return self._handoff_pipeline(
            video,
            audio,
            next_profile,
            current_pipeline,
            mux,
            run_label,
            source_cache=self._current_auto_align_source_cache(),
        )

    def auto_align_after_handoff(self, current_pipeline, profile):
        self._set_current_snapshot_jobs(current_pipeline, profile)

    def auto_align_persist_offset(self, offset):
        self._save_auto_offset(offset)

    def auto_align_handoff_failure(self, message):
        with self.lock:
            self.status["auto_align_msg"] = message
            self.status["stage"] = "running"

    def _set_current_snapshot_jobs(self, pipeline, profile):
        jobs = list(pipeline.snapshot_jobs or [])
        with self.lock:
            self.current_snapshot_jobs = jobs

    def _current_auto_align_source_cache(self):
        return self._auto_align_source_cache

    def _offset_input_args(self, input_args, offset_seconds):
        offset = float(offset_seconds or 0.0)
        if offset <= 0:
            return list(input_args)
        return ["-itsoffset", f"{offset:.3f}", *list(input_args)]

    def _input_has_video_stream(self, url, timeout, headers=None):
        text = str(url or "").strip()
        try:
            result = subprocess.run([
                "ffprobe", "-hide_banner", "-loglevel", "error",
                "-rw_timeout", str(timeout * 1_000_000),
                *self._http_input_options(text, headers),
                "-select_streams", "v",
                "-show_entries", "stream=codec_type",
                "-of", "csv=p=0",
                text,
            ], capture_output=True, text=True, timeout=timeout + 5, check=True)
        except Exception:
            return False
        return bool((result.stdout or "").strip())

    def _frame_capture_failure(self, url, timeout, headers, exc):
        stderr = ""
        if isinstance(exc, subprocess.CalledProcessError):
            stderr = (exc.stderr or "").strip()
        if "does not contain any stream" in stderr.lower() and not self._input_has_video_stream(url, timeout, headers):
            source_name = Path(str(url)).name if str(url or "").endswith(".m3u8") else str(url)
            return RuntimeError(f"frame source has no video stream: {source_name}")
        detail = stderr.splitlines()[-1].strip() if stderr else str(exc).strip()
        return RuntimeError(detail or "frame capture failed")

    def _extract_current_frame(self, url, out_path, timeout, headers=None, offset_seconds=0.0):
        timeout = min(coerce_int(timeout, DEFAULT_PROFILE["timeout_seconds"], minimum=5), FFMPEG_FAST_TIMEOUT_CAP_SECONDS)
        text = str(url or "")
        if text.endswith(".m3u8") and not text.startswith(("http://", "https://")) and not Path(text).exists():
            raise FileNotFoundError(f"frame source missing: {text}")
        try:
            subprocess.run([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                *self._offset_input_args(
                    self._fast_input_args(url, timeout, headers),
                    offset_seconds,
                ),
                "-frames:v", "1", "-update", "1", str(out_path),
            ], capture_output=True, text=True, timeout=timeout + 5, check=True)
        except Exception as exc:
            raise self._frame_capture_failure(url, timeout, headers, exc) from exc

    def _capture_frame_at_deadline(
        self,
        kind,
        url,
        out_path,
        timeout,
        headers,
        barrier,
        deadline_ns,
        offset_seconds=0.0,
        *,
        force_live_edge=False,
        media_time=0.0,
        anchor_time=0.0,
    ):
        barrier.wait()
        now_ns = time.monotonic_ns()
        if deadline_ns and now_ns < deadline_ns:
            time.sleep((deadline_ns - now_ns) / 1_000_000_000)
        started_at = time.monotonic()
        if force_live_edge or float(offset_seconds or 0.0) > 0:
            self._extract_frame_from_live_edge(url, offset_seconds, out_path, timeout, headers)
        else:
            self._extract_current_frame(url, out_path, timeout, headers, offset_seconds=0.0)
        finished_at = time.monotonic()
        return FrameCaptureResult(
            Path(out_path),
            kind,
            started_at,
            finished_at,
            media_time=float(media_time or 0.0),
            source=kind,
            anchor_time=float(anchor_time or 0.0),
            seconds_back=float(offset_seconds or 0.0),
        )

    def _capture_frame_pair(self, video, audio, tmpdir, timeout, *, deadline_delay=1.25, video_offset=0.0, audio_offset=0.0):
        tmpdir = Path(tmpdir)
        tmpdir.mkdir(parents=True, exist_ok=True)
        video_frame = tmpdir / "align_video.jpg"
        audio_frame = tmpdir / "align_audio.jpg"
        barrier = threading.Barrier(2)
        deadline_ns = time.monotonic_ns() + int(max(0.0, deadline_delay) * 1_000_000_000)
        capture_plan = self._shared_local_hls_capture_plan(
            video.url,
            audio.url,
            video_offset=video_offset,
            audio_offset=audio_offset,
        )
        if not capture_plan:
            raise RuntimeError("source cache unavailable: no shared stable local HLS anchor")
        force_live_edge = True
        shared_anchor_time = capture_plan["anchor_time"] if capture_plan else 0.0
        video_capture_offset = capture_plan["video_seconds_back"] if capture_plan else video_offset
        audio_capture_offset = capture_plan["audio_seconds_back"] if capture_plan else audio_offset
        video_media_time = capture_plan["video_target"] if capture_plan else 0.0
        audio_media_time = capture_plan["audio_target"] if capture_plan else 0.0
        with ThreadPoolExecutor(max_workers=2) as pool:
            futs = [
                pool.submit(
                    self._capture_frame_at_deadline,
                    "video",
                    video.url,
                    video_frame,
                    timeout,
                    video.headers,
                    barrier,
                    deadline_ns,
                    video_capture_offset,
                    force_live_edge=force_live_edge,
                    media_time=video_media_time,
                    anchor_time=shared_anchor_time,
                ),
                pool.submit(
                    self._capture_frame_at_deadline,
                    "audio",
                    audio.url,
                    audio_frame,
                    timeout,
                    audio.headers,
                    barrier,
                    deadline_ns,
                    audio_capture_offset,
                    force_live_edge=force_live_edge,
                    media_time=audio_media_time,
                    anchor_time=shared_anchor_time,
                ),
            ]
            results = []
            for fut in as_completed(futs):
                results.append(fut.result())
        by_kind = {item.kind: item for item in results}
        return by_kind["video"], by_kind["audio"]

    def _capture_alignment_frames(self, video, audio, tmpdir, timeout):
        video_cap, audio_cap = self._capture_frame_pair(video, audio, tmpdir, timeout)
        return video_cap.path, audio_cap.path

    def _pair_capture_uses_shared_anchor(self, left, right):
        return float(getattr(left, "anchor_time", 0.0) or 0.0) > 0 and float(getattr(right, "anchor_time", 0.0) or 0.0) > 0

    def _pair_capture_skew(self, left, right):
        if self._pair_capture_uses_shared_anchor(left, right):
            return abs(float(left.anchor_time) - float(right.anchor_time))
        return abs(left.started_at - right.started_at)

    def _pair_capture_finish_delta(self, left, right):
        if self._pair_capture_uses_shared_anchor(left, right):
            return 0.0
        return abs(left.finished_at - right.finished_at)

    def _playlist_window_duration(self, playlist_path):
        playlist = Path(playlist_path or "")
        try:
            lines = playlist.read_text(encoding="utf-8", errors="replace").splitlines()
        except FileNotFoundError:
            return 0.0
        duration = 0.0
        for raw in lines:
            match = HLS_EXTINF_RE.match(raw.strip())
            if not match:
                continue
            try:
                duration += float(match.group(1))
            except (TypeError, ValueError):
                continue
        return duration

    def _playlist_segment_durations(self, playlist_path):
        playlist = Path(playlist_path or "")
        try:
            lines = playlist.read_text(encoding="utf-8", errors="replace").splitlines()
        except FileNotFoundError:
            return []
        durations = []
        pending = None
        for raw in lines:
            line = raw.strip()
            match = HLS_EXTINF_RE.match(line)
            if match:
                try:
                    pending = float(match.group(1))
                except (TypeError, ValueError):
                    pending = None
                continue
            if not line or line.startswith("#"):
                continue
            if pending is None:
                continue
            durations.append(pending)
            pending = None
        return durations

    def _parse_program_date_time(self, value):
        text = str(value or "").strip()
        if not text:
            return None
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None

    def _playlist_program_time_window(self, playlist_path):
        playlist = Path(playlist_path or "")
        try:
            lines = playlist.read_text(encoding="utf-8", errors="replace").splitlines()
        except FileNotFoundError:
            return None
        cursor_ts = None
        pending_duration = None
        pending_pdt = None
        start_time = None
        end_time = None
        segment_count = 0
        for raw in lines:
            line = raw.strip()
            if not line:
                continue
            if line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
                pending_pdt = self._parse_program_date_time(line.split(":", 1)[1])
                continue
            match = HLS_EXTINF_RE.match(line)
            if match:
                try:
                    pending_duration = float(match.group(1))
                except (TypeError, ValueError):
                    pending_duration = None
                continue
            if line.startswith("#"):
                continue
            if pending_pdt is not None:
                cursor_ts = pending_pdt
            if pending_duration is None or cursor_ts is None:
                pending_duration = None
                pending_pdt = None
                continue
            segment_start = cursor_ts
            segment_end = cursor_ts + pending_duration
            if start_time is None:
                start_time = segment_start
            end_time = segment_end
            segment_count += 1
            cursor_ts = segment_end
            pending_duration = None
            pending_pdt = None
        if segment_count <= 0 or start_time is None or end_time is None:
            return None
        return PlaylistProgramWindow(start_time, end_time, segment_count)

    def _shared_local_hls_capture_plan(self, video_url, audio_url, *, video_offset=0.0, audio_offset=0.0):
        video_text = str(video_url or "").strip()
        audio_text = str(audio_url or "").strip()
        if not video_text or not audio_text:
            return None
        if video_text.startswith(("http://", "https://")) or audio_text.startswith(("http://", "https://")):
            return None
        if not video_text.endswith(".m3u8") or not audio_text.endswith(".m3u8"):
            return None
        video_window = self._playlist_program_time_window(video_text)
        audio_window = self._playlist_program_time_window(audio_text)
        if not video_window or not audio_window:
            return None

        anchor_time = min(video_window.end_time, audio_window.end_time) - ALIGN_CAPTURE_ANCHOR_MARGIN_SECONDS
        video_target = anchor_time - max(0.0, float(video_offset or 0.0))
        audio_target = anchor_time - max(0.0, float(audio_offset or 0.0))
        if video_target <= video_window.start_time or audio_target <= audio_window.start_time:
            return None

        video_seconds_back = video_window.end_time - video_target
        audio_seconds_back = audio_window.end_time - audio_target
        if video_seconds_back <= 0 or audio_seconds_back <= 0:
            return None

        return {
            "anchor_time": anchor_time,
            "video_target": video_target,
            "audio_target": audio_target,
            "video_seconds_back": video_seconds_back,
            "audio_seconds_back": audio_seconds_back,
        }

    def _local_hls_seek_plan(self, playlist_path, seconds_back):
        durations = self._playlist_segment_durations(playlist_path)
        if not durations:
            raise RuntimeError(f"playlist has no duration: {Path(playlist_path).name}")
        total_duration = sum(durations)
        target_time = max(0.0, total_duration - max(0.0, float(seconds_back or 0.0)))
        if target_time <= 0:
            return 0, 0.0

        starts = []
        elapsed = 0.0
        for duration in durations:
            starts.append(elapsed)
            elapsed += duration

        start_index = max(0, len(durations) - 1)
        for idx, segment_start in enumerate(starts):
            if target_time < segment_start + durations[idx]:
                start_index = idx
                break

        # Keep ffmpeg's post-input seek short; larger seeks on the rolling 4K
        # local cache reproduce the 20s verify timeout seen in production.
        max_local_seek = 6.0
        while start_index > 0 and (target_time - starts[start_index - 1]) <= max_local_seek:
            start_index -= 1

        seek_seconds = max(0.0, target_time - starts[start_index])
        return start_index, seek_seconds

    def _build_local_hls_capture_playlist(self, playlist_path, start_index):
        playlist = Path(playlist_path)
        try:
            lines = playlist.read_text(encoding="utf-8", errors="replace").splitlines()
        except FileNotFoundError as exc:
            raise FileNotFoundError(f"frame source missing: {playlist}") from exc

        header_lines = []
        segment_blocks = []
        pending_lines = []
        media_sequence = 0
        segment_tag_prefixes = (
            "#EXTINF",
            "#EXT-X-PROGRAM-DATE-TIME",
            "#EXT-X-DISCONTINUITY",
            "#EXT-X-BYTERANGE",
        )

        for raw in lines:
            line = raw.strip()
            if not line or line.startswith("#EXT-X-ENDLIST"):
                continue
            if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
                try:
                    media_sequence = int(line.split(":", 1)[1].strip())
                except (TypeError, ValueError):
                    media_sequence = 0
                continue
            if line.startswith("#") and not segment_blocks and not pending_lines and not line.startswith(segment_tag_prefixes):
                header_lines.append(line)
                continue
            if line.startswith("#"):
                pending_lines.append(line)
                continue
            segment_blocks.append([*pending_lines, line])
            pending_lines = []

        if not segment_blocks:
            raise RuntimeError(f"playlist has no segments: {playlist.name}")

        start_index = max(0, min(int(start_index), len(segment_blocks) - 1))
        output_lines = []
        if not header_lines or header_lines[0] != "#EXTM3U":
            output_lines.append("#EXTM3U")
        output_lines.extend(line for line in header_lines if line != "#EXTM3U")
        output_lines.append(f"#EXT-X-MEDIA-SEQUENCE:{media_sequence + start_index}")
        for block in segment_blocks[start_index:]:
            output_lines.extend(block)
        output_lines.append("#EXT-X-ENDLIST")

        tmp_playlist = playlist.parent / f".capture_{uuid.uuid4().hex}.m3u8"
        tmp_playlist.write_text("\n".join(output_lines) + "\n", encoding="utf-8")
        return tmp_playlist

    def _extract_frame_from_live_edge(self, url, seconds_back, out_path, timeout, headers=None):
        timeout = min(coerce_int(timeout, DEFAULT_PROFILE["timeout_seconds"], minimum=5), FFMPEG_FAST_TIMEOUT_CAP_SECONDS)
        seconds_back = max(0.0, float(seconds_back or 0.0))
        text = str(url or "").strip()
        is_http = text.startswith(("http://", "https://"))
        if not is_http and text.endswith(".m3u8"):
            playlist = Path(text)
            if not playlist.exists():
                raise FileNotFoundError(f"frame source missing: {text}")
            start_index, at = self._local_hls_seek_plan(playlist, seconds_back)
            tmp_playlist = self._build_local_hls_capture_playlist(playlist, start_index)
            try:
                subprocess.run([
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    *self._fast_input_args(str(tmp_playlist), timeout, headers),
                    "-ss", f"{at:.3f}",
                    "-frames:v", "1", "-update", "1", str(out_path),
                ], capture_output=True, text=True, timeout=timeout + 10, check=True)
            except Exception as exc:
                raise self._frame_capture_failure(text, timeout, headers, exc) from exc
            finally:
                tmp_playlist.unlink(missing_ok=True)
            return
        self._extract_current_frame(url, out_path, timeout, headers, offset_seconds=seconds_back)

    def _read_locked_clock(self, frame_path, roi, kind=None):
        if roi is None:
            return None
        parsed = self._ocr_time(frame_path, roi)
        if not parsed:
            if kind:
                self._set_ocr_diagnostic(kind, error="remote OCR recognized no valid clock")
                with self.lock:
                    self.status.setdefault("last_ocr_results", {})[kind] = {
                        "clock": None,
                        "error": "OCR failed",
                        "provider": "",
                        "note": "",
                    }
            return None
        result = ClockSample(0.0, parsed[0], parsed[1], roi, parsed[2] if len(parsed) > 2 else "", parsed[3] if len(parsed) > 3 else "")
        if kind:
            self._set_ocr_diagnostic(kind, provider=result.provider, note=result.note)
            with self.lock:
                self.status.setdefault("last_ocr_results", {})[kind] = {
                    "clock": result.text,
                    "game_time": result.game_time,
                    "updated_at": time.strftime("%H:%M:%S", time.localtime()),
                    "provider": result.provider,
                    "note": result.note,
                }
        return result

    def _read_top_quarter_clock(self, frame_path, kind=None):
        return self._read_locked_clock(frame_path, self._scoreboard_top_quarter_roi(), kind=kind)

    def _scoreboard_top_quarter_roi(self):
        return (0.0, 0.0, 1.0, 0.25)

    def _find_frame_clock(self, frame_path, *, full_frame=False, profile=None):
        if profile is None:
            profile = self.get_profile()
        providers = ocr_provider_order(profile)
        if not providers and not rapidocr_available():
            return None
        any_request_failure = False
        for idx, provider in enumerate(providers):
            if idx > 0 and full_frame:
                self.log(f"OCR primary '{providers[0]}' failed, fallback to '{provider}'")
            provider_result = self._try_ocr_provider(provider, frame_path, profile)
            found = coerce_clock_sample(provider_result.value)
            if found:
                return found
            if provider_result.request_failed:
                any_request_failure = True
                continue
            return None
        if providers and not any_request_failure:
            return None
        found = coerce_clock_sample(self._rapidocr_time(frame_path))
        if found:
            if providers and full_frame:
                self.log("OCR remote providers failed, fallback to 'rapidocr_local'")
            return found
        return None

    def _try_ocr_provider(self, provider, frame_path, profile):
        if provider == "ocrspace":
            return self._find_clock_via_ocrspace(frame_path, profile)
        elif provider == "custom":
            return self._find_clock_via_custom(frame_path, profile)
        return OcrProviderResult()

    def _read_verify_clock(self, frame_path, kind, profile, monitor):
        return self._read_top_quarter_clock(frame_path)

    def _verify_alignment_candidate(self, video, audio, profile, monitor, candidate):
        if not audio or not audio.url:
            monitor.verify_video_clock = ""
            monitor.verify_audio_clock = ""
            monitor.verify_delta = None
            monitor.verify_message = "video-only; no audio clock to compare"
            return False, "video-only; no audio clock to compare"
        threshold = coerce_float(profile.get("auto_align_threshold"), DEFAULT_PROFILE["auto_align_threshold"], minimum=0.1)
        timeout = coerce_int(profile.get("timeout_seconds"), DEFAULT_PROFILE["timeout_seconds"], minimum=5)
        previous_stage = ""
        with self.lock:
            previous_stage = self.status.get("stage", "")
        try:
            readings, error = self.auto_align_collect_probe_readings(
                video,
                audio,
                {**profile, "offset_seconds": candidate},
                1,
                timeout,
                0.0,
                stage="verify",
            )
            if error and not readings:
                monitor.verify_video_clock = ""
                monitor.verify_audio_clock = ""
                monitor.verify_delta = None
                monitor.verify_message = f"verify capture failed: {error}"
                return False, f"verify capture failed: {error}"
            if not readings:
                monitor.verify_video_clock = ""
                monitor.verify_audio_clock = ""
                monitor.verify_delta = None
                monitor.verify_message = "verify OCR unstable"
                return False, "verify OCR unstable"
            video_sample, audio_sample = readings[0]
            if not video_sample or not audio_sample:
                monitor.verify_video_clock = video_sample.text if video_sample else ""
                monitor.verify_audio_clock = audio_sample.text if audio_sample else ""
                monitor.verify_delta = None
                monitor.verify_message = "verify OCR unstable"
                return False, "verify OCR unstable"
            delta = abs(video_sample.game_time - audio_sample.game_time)
            monitor.verify_video_clock = video_sample.text
            monitor.verify_audio_clock = audio_sample.text
            monitor.verify_delta = delta
            if delta <= threshold:
                monitor.verify_message = (
                    f"verified offset {candidate:.3f}s delta={delta:.1f}s "
                    f"v={video_sample.text} a={audio_sample.text}"
                )
                return True, (
                    f"verified offset {candidate:.3f}s delta={delta:.1f}s "
                    f"v={video_sample.text} a={audio_sample.text}"
                )
            monitor.verify_message = (
                f"verify mismatch delta={delta:.1f}s "
                f"v={video_sample.text} a={audio_sample.text}"
            )
            return False, (
                f"verify mismatch delta={delta:.1f}s "
                f"v={video_sample.text} a={audio_sample.text}"
            )
        finally:
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
        self._auto_align_source_cache = source_cache
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
        last_runtime_prune_check = 0.0
        auto_align = AutoAlignController(
            self,
            video,
            audio,
            profile,
            pipeline_video,
            pipeline_audio,
            current_pipeline,
            mux,
            HandoffDeferred,
        )
        auto_align.run_id = run_id
        auto_align.first_segment_deadline = first_segment_deadline
        while not self.stop_event.is_set():
            auto_align_profile = auto_align.refresh_profile()
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
            align_allowed_now = self._auto_align_allowed_by_schedule(auto_align_profile)
            now_ts = time.time()
            if not source_cache:
                auto_align.set_state(ALIGN_STATE_DISABLED, "自动对齐依赖本地缓存；当前已暂停", now_ts=now_ts)
                self._set_align_monitor_status(auto_align.monitor)
                time.sleep(2)
                continue
            if not align_allowed_now and auto_align.monitor.state != ALIGN_STATE_DISABLED:
                auto_align.monitor.state = ALIGN_STATE_DISABLED
                self._set_align_monitor_status(auto_align.monitor, "非比赛时间：直播继续，暂停自动截图和对齐")
            if not align_allowed_now or not ocr_provider_ready(auto_align_profile):
                self._set_align_monitor_status(auto_align.monitor)
                time.sleep(2)
                continue
            probe_result = auto_align.maybe_probe(now_ts=now_ts, mtime=mtime, timeout=timeout)
            current_pipeline = auto_align.current_pipeline
            mux = auto_align.mux
            auto_align_profile = auto_align.profile
            first_segment_deadline = auto_align.first_segment_deadline or first_segment_deadline
            if probe_result:
                return probe_result
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
            if event.start_ts <= now_ts <= event.end_ts and self._schedule_event_enabled(event, profile):
                active = event
                break
        next_match = None
        for event in events:
            if not self._schedule_event_enabled(event, profile):
                continue
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
            "selected_event_ids": sorted(self._schedule_selected_event_ids(profile)),
            "upcoming_matches": self._schedule_upcoming_matches(profile),
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
                self._ensure_schedule_recording_started()
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
            self._ensure_schedule_recording_stopped()
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
        timeout = min(coerce_int(timeout, DEFAULT_PROFILE["timeout_seconds"], minimum=5), FFMPEG_FAST_TIMEOUT_CAP_SECONDS)
        subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            *self._fast_input_args(url, timeout, headers),
            "-ss", f"{at:.3f}",
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
                *self._stable_input_args(url, timeout, headers),
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
                *self._stable_input_args(url, timeout, headers),
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
        normalized = (text or "").strip().replace("O", "0").replace("o", "0").replace("＋", "+")
        normalized = re.sub(r"(?i)(?<!\d)\+\s*mins?\.?\s*[:：.]?\s*([0-9]{1,2})(?!\d)", r"+\1", normalized)
        normalized = re.sub(r"(?i)(?<!\d)mins?\.?\s*[:：.]?\s*([0-9]{1,2})(?!\d)", r"+\1", normalized)
        return normalized

    def _extract_timer_text(self, text):
        normalized = self._normalize_ocr_text(text).replace("：", ":")
        normalized = re.sub(r"(?<=\d)[.](?=\d)", ":", normalized)
        normalized = re.sub(r"\s*\+\s*", "+", normalized)
        normalized = re.sub(r"\s+", " ", normalized).strip()
        compact = normalized.replace(" ", "")
        for candidate in (compact, normalized):
            for pattern in TIMER_TEXT_PATTERNS:
                match = pattern.search(candidate)
                if match:
                    return match.group(1)
        return ""

    def _parse_stoppage_ocr_text(self, text):
        normalized = self._normalize_ocr_text(text)
        cleaned = re.sub(r"\s+", "", normalized)

        m = STOPPAGE_RE.search(cleaned)
        if m:
            parsed = self._combine_stoppage_parts(m.group(1), "00", m.group(2), m.group(3), m.group(0))
            if parsed:
                return parsed

        # Scoreboards often OCR as either "45:00 0:32+4" or three separate lines:
        # "45:00", "0:32", "+4". The elapsed timer is the actual game time offset;
        # the trailing +4/+5 is only the announced total stoppage allowance.
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
        return OcrProviderResult()

    def _ocrspace_candidate_rois(self, roi, profile):
        candidates = []
        top_quarter = self._scoreboard_top_quarter_roi()
        try:
            base = parse_roi(roi)
        except (TypeError, ValueError):
            base = None
        if base:
            base = (
                max(top_quarter[0], base[0]),
                max(top_quarter[1], base[1]),
                min(base[2], top_quarter[2]),
                min(base[3], max(0.01, top_quarter[3] - max(top_quarter[1], base[1]))),
            )
        if base:
            candidates.append(base)
        candidates.extend([
            (0.0, 0.0, 1.0, 0.18),
            top_quarter,
        ])
        seen = set()
        result = []
        for item in candidates:
            item = (
                max(0.0, min(1.0, float(item[0]))),
                max(0.0, min(top_quarter[3], float(item[1]))),
                max(0.01, min(1.0, float(item[2]))),
                max(0.01, min(top_quarter[3] - max(0.0, min(top_quarter[3], float(item[1]))), float(item[3]))),
            )
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

    def _parse_ocr_text_candidates(self, raw_text, *, allow_timer_only=False):
        text = str(raw_text or "").strip()
        if not text:
            return None
        parsed = self._parse_clock_text(text)
        if parsed:
            return parsed
        timer = self._extract_timer_text(text)
        if timer:
            parsed = self._parse_clock_text(timer)
            if parsed:
                return parsed
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        for line in lines:
            parsed = self._parse_clock_text(line)
            if parsed:
                return parsed
            timer = self._extract_timer_text(line)
            if timer:
                parsed = self._parse_clock_text(timer)
                if parsed:
                    return parsed
        return None

    def _custom_ocr_prompt(self):
        return (
            "Read the football match timer in this image. "
            "Return ONLY the timer text exactly as shown, such as 41:12, 90:00, 45:00+02:30, 45:00 0:32+4. "
            "If no timer is visible, return an empty string."
        )

    def _send_custom_ocr_crop(self, crop_img, endpoint, api_key, model, prompt=None):
        import cv2 as _cv2, base64, json as _json, urllib.request
        if crop_img is None or crop_img.size == 0:
            return "", False
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
                return reply, False
        if last_exc is not None:
            self.log(f"Custom OCR API error: {last_exc}")
            return "", True
        return "", False

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

    def _set_ocr_diagnostic(self, kind, *, provider="", note="", error=""):
        if not kind:
            return
        route = ""
        if provider == "rapidocr_local" and note:
            route = "remote_request_failed -> rapidocr_local"
        elif provider:
            route = provider
        with self.lock:
            self.status.setdefault("last_ocr_diagnostic", {})[kind] = {
                "provider": provider or "",
                "note": note or "",
                "error": error or "",
                "route": route,
            }

    def _rapidocr(self):
        if self.rapidocr_engine is not None:
            return self.rapidocr_engine
        if not rapidocr_available():
            return None
        with self.ocr_lock:
            if self.rapidocr_engine is None:
                self.rapidocr_engine = RapidOCR(config_path=str(RAPIDOCR_CONFIG_PATH))
        return self.rapidocr_engine

    def _ocr_time(self, frame_path, roi, scale=6):
        profile = self.get_profile()
        providers = ocr_provider_order(profile)
        if not providers and not rapidocr_available():
            return None
        any_request_failure = False
        any_remote_attempt = False
        remote_failure_note = ""
        for idx, provider in enumerate(providers):
            if not ocr_provider_ready_for(provider, profile):
                continue
            any_remote_attempt = True
            if idx > 0:
                self.log(f"OCR primary '{providers[0]}' failed, ROI fallback to '{provider}'")
            provider_result = self._ocr_region_with_provider(provider, frame_path, roi, profile)
            if provider_result.request_failed:
                any_request_failure = True
                remote_failure_note = "remote request failed"
                continue
            result = provider_result.value
            if result:
                parsed_text = self._parse_ocr_text_candidates(result[1], allow_timer_only=True)
                if parsed_text:
                    return parsed_text.game_time, parsed_text.text, provider, ""
                remote_failure_note = "remote OCR returned text but no valid clock"
            else:
                remote_failure_note = "remote OCR recognized no valid clock"
            return None
        if providers and not any_request_failure:
            return None
        rapid = self._rapidocr_time(frame_path)
        if rapid:
            note = "fallback after remote request failure" if any_remote_attempt else ""
            return rapid[0], rapid[1], "rapidocr_local", note
        return None

    def _rapidocr_variants(self, crop):
        variants = []
        raw = crop
        h, w = raw.shape[:2]
        if w < 960:
            scale = 960.0 / max(w, 1)
            raw = cv2.resize(raw, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        variants.append(raw)
        gray = cv2.cvtColor(raw, cv2.COLOR_BGR2GRAY)
        norm = cv2.normalize(gray, None, 0, 255, cv2.NORM_MINMAX)
        variants.append(cv2.cvtColor(norm, cv2.COLOR_GRAY2BGR))
        otsu = cv2.threshold(norm, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
        variants.append(cv2.cvtColor(otsu, cv2.COLOR_GRAY2BGR))
        inv = cv2.threshold(norm, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
        variants.append(cv2.cvtColor(inv, cv2.COLOR_GRAY2BGR))
        return variants

    def _rapidocr_lines(self, result):
        lines = []
        if not result:
            return lines
        raw_items = result[0] if isinstance(result, tuple) else result
        if not raw_items:
            return lines
        for item in raw_items:
            if not isinstance(item, (list, tuple)):
                continue
            if len(item) >= 3 and isinstance(item[1], str):
                lines.append(str(item[1]).strip())
            elif len(item) >= 2 and isinstance(item[0], str):
                lines.append(str(item[0]).strip())
        return [line for line in lines if line]

    def _rapidocr_time(self, frame_path):
        engine = self._rapidocr()
        if engine is None:
            return None
        self._record_ocr_request("rapidocr_local")
        fallback = None
        for roi in RAPIDOCR_PRESET_ROIS:
            crop = self._roi_crop(frame_path, roi)
            if crop is None:
                continue
            for variant in self._rapidocr_variants(crop):
                result, _elapsed = engine(variant)
                text = " ".join(self._rapidocr_lines(result)).strip()
                parsed = self._parse_ocr_text_candidates(text, allow_timer_only=True)
                if parsed:
                    if parsed.kind == "stoppage":
                        return parsed.game_time, parsed.text
                    if fallback is None:
                        fallback = (parsed.game_time, parsed.text)
        return fallback


    def _ocr_send_ocrspace_jpeg(self, jpeg_buf, api_key):
        """Send a JPEG buffer to OCR.space, return OCR text payload plus request-failure state."""
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
            return OcrProviderResult(request_failed=True)
        if result.get("IsErroredOnProcessing") or result.get("OCRExitCode") != 1:
            err = result.get("ErrorMessage", ["unknown"])[0] if isinstance(result.get("ErrorMessage"), list) else str(result.get("ErrorMessage", "unknown"))
            self.log(f"OCR.space processing error: {err}")
            return OcrProviderResult(request_failed=True)
        parsed_items = result.get("ParsedResults") or []
        if not parsed_items:
            return OcrProviderResult()
        parsed_text = (parsed_items[0].get("ParsedText") or "").strip()
        if not parsed_text:
            return OcrProviderResult()
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
            if ratio is not None and self._parse_ocr_text_candidates(line_text, allow_timer_only=False):
                left_ratio = ratio
                break
        if left_ratio is None:
            left_ratio = fallback_ratio
        return OcrProviderResult(value=(parsed_text, left_ratio))

    def _ocr_via_ocrspace(self, frame_path, roi, profile):
        """Send one or more crops to OCR.space and return parsed clock."""
        api_key = coerce_text(profile.get("ocrspace_api_key", "")).strip()
        if not api_key:
            return OcrProviderResult()
        img = cv2.imread(str(frame_path))
        if img is None:
            return OcrProviderResult()
        any_request_failure = False
        for candidate_roi in self._ocrspace_candidate_rois(roi, profile):
            buf = self._prepare_ocrspace_crop(img, candidate_roi)
            if buf is None:
                continue
            result = self._ocr_send_ocrspace_jpeg(buf, api_key)
            if result.request_failed:
                any_request_failure = True
                continue
            if result.value is None:
                continue
            raw_text, _left_ratio = result.value
            parsed = self._parse_ocr_text_candidates(raw_text, allow_timer_only=True)
            if parsed:
                return OcrProviderResult(value=(parsed.game_time, parsed.text))
        return OcrProviderResult(request_failed=any_request_failure)

    def _find_clock_via_ocrspace(self, frame_path, profile):
        """Read the timer only from the top quarter of the frame."""
        api_key = coerce_text(profile.get("ocrspace_api_key", "")).strip()
        if not api_key:
            return OcrProviderResult()
        img = cv2.imread(str(frame_path))
        if img is None:
            return OcrProviderResult()
        top_quarter_roi = self._scoreboard_top_quarter_roi()
        buf = self._prepare_ocrspace_crop(img, top_quarter_roi)
        if buf is None:
            return OcrProviderResult()
        result = self._ocr_send_ocrspace_jpeg(buf, api_key)
        if result.request_failed:
            return OcrProviderResult(request_failed=True)
        if result.value is None:
            return OcrProviderResult()
        raw_text, _left_ratio = result.value
        parsed = self._parse_ocr_text_candidates(raw_text, allow_timer_only=True)
        if parsed:
            return OcrProviderResult(value=(parsed.game_time, parsed.text, top_quarter_roi))
        return OcrProviderResult()

    def _ocr_via_custom(self, frame_path, roi, profile):
        """Send cropped region to custom OpenAI-compatible API and return parsed clock."""
        endpoint = coerce_text(profile.get("ocr_custom_endpoint", "")).strip().rstrip("/")
        api_key = coerce_text(profile.get("ocr_api_key", "")).strip()
        model = coerce_text(profile.get("ocr_custom_model", DEFAULT_PROFILE.get("ocr_custom_model", "gpt-4o"))).strip()
        if not api_key or not endpoint:
            return OcrProviderResult()
        img = cv2.imread(str(frame_path))
        if img is None:
            return OcrProviderResult()
        h, w = img.shape[:2]
        x, y, rw, rh = roi
        crop = img[int(y*h):int((y+rh)*h), int(x*w):int((x+rw)*w)]
        reply, request_failed = self._send_custom_ocr_crop(crop, endpoint, api_key, model)
        if not reply:
            return OcrProviderResult(request_failed=request_failed)
        parsed = self._parse_ocr_text_candidates(reply, allow_timer_only=True)
        if parsed:
            return OcrProviderResult(value=(parsed.game_time, parsed.text))
        return OcrProviderResult()

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
            for current in ocr_provider_order(profile):
                if not ocr_provider_ready_for(current, profile):
                    continue
                label = ocr_provider_label(current)
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
                    break
                if not result:
                    messages.append(f"{label} 识别失败")
            if not success and rapidocr_available():
                try:
                    result = self._rapidocr_time(tmp_path)
                except Exception as exc:
                    result = None
                    messages.append(f"RapidOCR 异常: {str(exc)[:200]}")
                if result:
                    messages.append(f"RapidOCR 识别成功: {result[1]}")
                    success = True
                else:
                    messages.append("RapidOCR 识别失败")
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
            reply, _request_failed = self._send_custom_ocr_crop(_crop_from_roi(roi), endpoint, api_key, model)
            if not reply:
                return None
            return self._parse_ocr_text_candidates(reply, allow_timer_only=True)
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


    def _snapshot_roi_for_kind(self, profile, kind):
        return self._scoreboard_top_quarter_roi()

    def _write_snapshot_file(self, frame_path, kind, roi):
        with self.snapshot_file_lock:
            SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
            out = SNAPSHOT_DIR / f"{kind}_snapshot.jpg"
            tmp_out = SNAPSHOT_DIR / f".{kind}_snapshot.tmp.jpg"
            crop = self._roi_crop(frame_path, roi)
            if crop is not None:
                cv2.imwrite(str(tmp_out), crop, [int(cv2.IMWRITE_JPEG_QUALITY), 95])
            else:
                shutil.copyfile(frame_path, tmp_out)
            os.replace(tmp_out, out)
            self._prune_snapshots()
            return out

    def _save_snapshot_without_ocr(self, frame_path, kind, profile, source_name, *, suffix="manual"):
        roi = self._snapshot_roi_for_kind(profile, kind)
        out = self._write_snapshot_file(frame_path, kind, roi)
        with self.lock:
            self.status["last_snapshot_at"] = now()
        self.log(f"captured {kind} {suffix} snapshot: {out.name}")
        return {"kind": kind, "url": f"/snapshots/{out.name}", "source": source_name, "mode": suffix, "clock": ""}

    def _save_snapshot_from_frame(self, frame_path, kind, profile, source_name):
        roi = self._snapshot_roi_for_kind(profile, kind)
        parsed = self._ocr_time(frame_path, roi)
        suffix = "timer" if parsed else "full"
        if not parsed:
            suffix = "full"
            self._set_ocr_diagnostic(kind, error="remote OCR recognized no valid clock")
        else:
            self._set_ocr_diagnostic(kind, provider=parsed[2] if len(parsed) > 2 else "", note=parsed[3] if len(parsed) > 3 else "")
        out = self._write_snapshot_file(frame_path, kind, roi)
        diagnostic = {}
        with self.lock:
            diagnostic = dict((self.status.get("last_ocr_diagnostic") or {}).get(kind) or {})
        with self.lock:
            self.status["last_snapshot_at"] = now()
            self.status.setdefault("last_ocr_results", {})[kind] = {
                "clock": parsed[1] if parsed else None,
                "game_time": parsed[0] if parsed else None,
                "updated_at": time.strftime("%H:%M:%S", time.localtime()),
                "error": "" if parsed else "OCR failed",
                "provider": diagnostic.get("provider", ""),
                "note": diagnostic.get("note", ""),
                "route": diagnostic.get("route", ""),
                "detail_error": diagnostic.get("error", ""),
            }
        detail = parsed[1] if parsed else "full frame"
        self.log(f"captured {kind} {suffix} snapshot: {out.name} ({detail})")
        return {"kind": kind, "url": f"/snapshots/{out.name}", "source": source_name, "mode": suffix, "clock": parsed[1] if parsed else ""}

    def _save_probe_snapshot_result(self, frame_path, kind, profile, sample, source_name, *, suffix="timer"):
        frame_path = Path(frame_path)
        if not frame_path.exists():
            self.log(f"skip {kind} {suffix} snapshot: missing frame {frame_path.name}")
            return None
        roi = sample.roi if sample and sample.roi else self._snapshot_roi_for_kind(profile, kind)
        out = self._write_snapshot_file(frame_path, kind, roi)
        if sample:
            self._set_ocr_diagnostic(kind, provider=sample.provider, note=sample.note)
        else:
            self._set_ocr_diagnostic(kind, error="remote OCR recognized no valid clock")
        diagnostic = {}
        with self.lock:
            diagnostic = dict((self.status.get("last_ocr_diagnostic") or {}).get(kind) or {})
        with self.lock:
            self.status["last_snapshot_at"] = now()
            self.status.setdefault("last_ocr_results", {})[kind] = {
                "clock": sample.text if sample else None,
                "game_time": sample.game_time if sample else None,
                "updated_at": time.strftime("%H:%M:%S", time.localtime()),
                "error": "" if sample else "OCR failed",
                "provider": diagnostic.get("provider", ""),
                "note": diagnostic.get("note", ""),
                "route": diagnostic.get("route", ""),
                "detail_error": diagnostic.get("error", ""),
            }
        detail = sample.text if sample else "full frame"
        self.log(f"captured {kind} {suffix} snapshot: {out.name} ({detail})")
        return {"kind": kind, "url": f"/snapshots/{out.name}", "source": source_name, "mode": suffix, "clock": sample.text if sample else ""}

    def _save_alignment_pair_snapshots(self, video_frame, audio_frame, profile, video_sample, audio_sample, *, stage):
        video_kind = "video"
        audio_kind = "audio"
        if stage == "candidate":
            video_kind = "cache_video"
            audio_kind = "cache_audio"
        self._save_probe_snapshot_result(video_frame, video_kind, profile, video_sample, f"auto-align {stage} video")
        self._save_probe_snapshot_result(audio_frame, audio_kind, profile, audio_sample, f"auto-align {stage} audio")

    def _capture_url_snapshot(self, kind, url, source_name, profile, headers=None):
        if not url:
            raise RuntimeError(f"{kind} URL is empty")
        timeout = coerce_int(profile.get("timeout_seconds"), DEFAULT_PROFILE["timeout_seconds"], minimum=5)
        headers = dict(headers or parse_header_lines(profile.get(f"{kind}_headers", "")))
        timeout = min(timeout, FFMPEG_FAST_TIMEOUT_CAP_SECONDS)
        tmp_path = self._capture_snapshot_frame(self._snapshot_input(url, timeout, headers), timeout)
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
        jobs = [("video", self._snapshot_input(video.url, timeout, video.headers), self.status.get("active_channel") or "active video")]
        if audio and audio.url:
            jobs.append(("audio", self._snapshot_input(audio.url, timeout, audio.headers), self.status.get("active_audio_channel") or profile.get("audio_channel") or "active audio"))
        return jobs

    def _capture_snapshot_frames(self, jobs, profile):
        jobs = [job for job in jobs if job[1]]
        frames = {}
        errors = {}
        if not jobs:
            return frames, {"snapshot": "no snapshot URLs available"}
        timeout = min(coerce_int(profile.get("timeout_seconds"), DEFAULT_PROFILE["timeout_seconds"], minimum=5), FFMPEG_FAST_TIMEOUT_CAP_SECONDS)
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

    def _save_snapshot_frames(self, frames, profile, *, run_ocr=True, suffix="manual"):
        results = {}
        errors = {}
        for kind in SNAPSHOT_KINDS:
            if kind not in frames:
                continue
            frame_path, source_name = frames[kind]
            try:
                if run_ocr:
                    results[kind] = self._save_snapshot_from_frame(frame_path, kind, profile, source_name)
                else:
                    results[kind] = self._save_snapshot_without_ocr(frame_path, kind, profile, source_name, suffix=suffix)
            except Exception as exc:
                errors[kind] = str(exc)
        return results, errors

    def _cleanup_snapshot_frames(self, frames):
        for frame_path, _source_name in frames.values():
            frame_path.unlink(missing_ok=True)

    def _capture_snapshot_jobs(self, jobs, profile):
        frames, errors = self._capture_snapshot_frames(jobs, profile)
        try:
            results, save_errors = self._save_snapshot_frames(frames, profile, run_ocr=False, suffix="manual")
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
                jobs.append(("cache_video", self._snapshot_input(video_channel.url, timeout, video_channel.headers), f"{video_channel.name} 原始"))
                try:
                    audio_channel = self._resolve_snapshot_channel("audio", profile, force=True)
                except Exception:
                    audio_channel = None
                if audio_channel and audio_channel.url:
                    jobs.append(("cache_audio", self._snapshot_input(audio_channel.url, timeout, audio_channel.headers), f"{audio_channel.name} 原始"))
            results, errors = self._capture_snapshot_jobs(jobs, profile)
        finally:
            self.snapshot_lock.release()
        if errors and not results:
            raise RuntimeError("; ".join(f"{kind}: {msg}" for kind, msg in errors.items()))
        return {
            "snapshots": [results[kind] for kind in SNAPSHOT_KINDS if kind in results],
            "errors": errors,
        }

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

    def _schedule_recording_enabled(self, profile=None):
        current = profile or self.get_profile()
        return parse_bool(current.get("schedule_recording_enabled", DEFAULT_PROFILE.get("schedule_recording_enabled", False)))

    def _schedule_selected_event_ids(self, profile=None):
        current = profile or self.get_profile()
        return {
            str(item).strip()
            for item in (current.get("schedule_selected_event_ids", []) or [])
            if str(item).strip()
        }

    def _schedule_upcoming_matches(self, profile):
        tz = self._schedule_tz(profile)
        today = datetime.now(tz).date()
        tomorrow = today + timedelta(days=1)
        items = []
        selected = self._schedule_selected_event_ids(profile)
        for match in list(self.schedule_events):
            start = datetime.fromtimestamp(match.start_ts, timezone.utc).astimezone(tz)
            end = datetime.fromtimestamp(match.end_ts, timezone.utc).astimezone(tz)
            if start.date() not in {today, tomorrow} and end.date() not in {today, tomorrow}:
                continue
            items.append({
                "event_id": match.event_id,
                "name": match.name,
                "short_name": match.short_name,
                "window_start": start.isoformat(timespec="minutes"),
                "window_end": end.isoformat(timespec="minutes"),
                "state": match.state,
                "selected": match.event_id in selected,
            })
        return items

    def _schedule_event_enabled(self, match, profile):
        if not match:
            return False
        selected = self._schedule_selected_event_ids(profile)
        if not selected:
            return True
        return match.event_id in selected

    def _ensure_schedule_recording_started(self):
        if not self._schedule_recording_enabled():
            return
        with self.recording_lock:
            session = self.recording_session
            if session and session.status in {"starting", "running", "stopping"}:
                return
        try:
            self.start_recording({"label": self.status.get("active_channel") or self.get_profile().get("channel_name") or "schedule recording"})
            self.log("schedule: recording started")
        except Exception as exc:
            self.log(f"schedule: recording start skipped: {exc}")

    def _ensure_schedule_recording_stopped(self):
        if not self._schedule_recording_enabled():
            return
        with self.recording_lock:
            session = self.recording_session
            if not session or session.status not in {"starting", "running", "stopping"}:
                return
        try:
            self.stop_recording()
            self.log("schedule: recording stopped")
        except Exception as exc:
            self.log(f"schedule: recording stop skipped: {exc}")

    def merge_recording(self, session_id, output_format="mkv"):
        session_id = str(session_id or "").strip()
        if not session_id:
            raise RuntimeError("missing session_id")
        meta = self._load_recording_meta(session_id)
        if meta.get("status") in {"starting", "running", "stopping"}:
            raise RuntimeError("recording is still running")
        output_format = str(output_format or "mkv").strip().lower()
        if output_format != "mkv":
            raise RuntimeError("output_format must be mkv")
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

    def delete_recording(self, session_id):
        session_id = str(session_id or "").strip()
        if not session_id:
            raise RuntimeError("missing session_id")
        session_dir = self._recording_dir(session_id)
        if not session_dir.exists():
            raise RuntimeError("recording does not exist")
        with self.recording_lock:
            active = self.recording_session
            if active and active.session_id == session_id and active.status in {"starting", "running", "stopping"}:
                raise RuntimeError("recording is still running")
        shutil.rmtree(session_dir, ignore_errors=False)
        with self.recording_lock:
            active = self.recording_session
            if active and active.session_id == session_id:
                self.recording_session = None
        self.log(f"recording deleted: {session_id}")
        return {"ok": True, "session_id": session_id}

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
                payload = dict(data or {})
                clean = parse_bool(payload.pop("clean", False))
                MANAGER.restart(payload if payload else None, clean=clean)
                return self.send_json(MANAGER.get_status())
            if path == "/api/stop":
                MANAGER.stop(source="manual")
                return self.send_json(MANAGER.get_status())
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
            if path == "/api/recording/delete":
                return self.send_json(MANAGER.delete_recording(data.get("session_id", "")))
            if path == "/api/clear":
                return self.send_json(MANAGER.clear_runtime(data.get("target", "")))
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
