#!/usr/bin/env python3
import json
import contextlib
import math
import mimetypes
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
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

import cv2


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
CLOCK_RE = re.compile(r"(?<!\d)([0-9]{1,3})[:：.]([0-9]{2})(?!\d)")
STOPPAGE_RE = re.compile(r"(?<!\d)([0-9]{1,3})(?::00)?\+([0-9]{1,2})(?:[:：.]([0-9]{2})(?!\d)|(?![:：.\d]))")
STOPPAGE_BASE_RE = re.compile(r"(?<!\d)([0-9]{1,3})(?:[:：.]([0-9]{2}))?(?!\d)")
ADDED_TIME_RE = re.compile(r"(?<!\d)\+([0-9]{1,2})(?:[:：.]([0-9]{2})(?!\d)|(?![:：.\d]))")
ADDED_LINE_RE = re.compile(r"^\+?([0-9]{1,2})(?:[:：.]([0-9]{2}))?$")
ELAPSED_ADDED_TIME_RE = re.compile(r"(?<![+\d])([0-9]{1,2})[:：.]([0-5][0-9])(?:\+[0-9]{1,2})?(?!\d)")
ADJACENT_STOPPAGE_BASES = {45, 90, 105, 120}
DEFAULT_VIDEO_TIMER_ROI_PRESETS = "\n".join([
    "0.132,0.055,0.078,0.140",
    "0.333,0.058,0.080,0.140",
    "0.114,0.049,0.077,0.077",
    "0.111,0.000,0.077,0.185",
])
DEFAULT_AUDIO_TIMER_ROI_PRESETS = "0.824,0.080,0.078,0.140"


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
    auto_align_outside_match = env_bool(
        "AUTO_ALIGN_OUTSIDE_MATCH",
        not env_bool("ALIGN_ONLY_DURING_MATCH", True),
    )
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
        "offset_seconds": load_offset_default(),
        "retry_limit": int(env("RETRY_LIMIT", "3") or 3),
        "timeout_seconds": int(env("TIMEOUT_SECONDS", "25") or 25),
        "segment_time": float(env("SEGMENT_TIME", "4") or 4),
        "playlist_size": int(env("PLAYLIST_SIZE", "30") or 30),
        "hls_segment_type": env("HLS_SEGMENT_TYPE", "fmp4").lower(),
        "public_base_url": env("PUBLIC_BASE_URL", ""),
        "channel_id": env("CHANNEL_ID", "cctv5-4k-cn"),
        "channel_name": env("CHANNEL_NAME", "CCTV5 4K Chinese"),
        "channel_number": env("CHANNEL_NUMBER", "5"),
        "channel_group": env("CHANNEL_GROUP", "Sports"),
        "auto_align_enabled": env_bool("AUTO_ALIGN_ENABLED", True),
        "auto_align_interval": int(env("AUTO_ALIGN_INTERVAL", "60") or 60),
        "auto_align_samples": int(env("AUTO_ALIGN_SAMPLES", "3") or 3),
        "auto_align_step": float(env("AUTO_ALIGN_STEP", "1") or 1),
        "auto_align_threshold": float(env("AUTO_ALIGN_THRESHOLD", "1") or 1),
        "auto_align_max_offset": float(env("AUTO_ALIGN_MAX_OFFSET", "180") or 180),
        "auto_align_relocate_attempts": int(env("AUTO_ALIGN_RELOCATE_ATTEMPTS", "3") or 3),
        "auto_align_stop_after_aligned": env_bool("AUTO_ALIGN_STOP_AFTER_ALIGNED", False),
        "snapshot_interval": int(env("SNAPSHOT_INTERVAL", "60") or 60),
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
        "auto_align_outside_match": auto_align_outside_match,
        "align_only_during_match": not auto_align_outside_match,
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


def hls_segment_type(profile):
    value = str(profile.get("hls_segment_type", DEFAULT_PROFILE.get("hls_segment_type", "fmp4")) or "fmp4").strip().lower()
    return "mpegts" if value in ("ts", "mpegts") else "fmp4"


def hls_segment_ext(profile):
    return ".ts" if hls_segment_type(profile) == "mpegts" else ".m4s"


def effective_hls_segment_type(profile, prepared=None):
    if prepared and prepared.compatible_mux:
        return "mpegts"
    return hls_segment_type(profile)


def effective_hls_segment_ext(profile, prepared=None):
    return ".ts" if effective_hls_segment_type(profile, prepared) == "mpegts" else ".m4s"


def strip_dovi_rpu():
    return env_bool("STRIP_DOVI_RPU", True)


def output_audio_codec():
    codec = env("OUTPUT_AUDIO_CODEC", "aac").strip().lower()
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


DEFAULT_PROFILE = make_default_profile()

URL_SAVE_KEYS = {
    "video_url",
    "audio_url",
    "public_base_url",
}
RUNTIME_AUTO_ALIGN_KEYS = {
    "auto_align_enabled",
    "auto_align_interval",
    "auto_align_samples",
    "auto_align_step",
    "auto_align_threshold",
    "auto_align_max_offset",
    "auto_align_relocate_attempts",
    "auto_align_stop_after_aligned",
    "snapshot_interval",
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
    "auto_align_outside_match",
    "align_only_during_match",
}

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


@dataclass(frozen=True)
class ClockCandidate:
    diff: float
    offset: float
    video: ClockSample
    audio: ClockSample


class HandoffDeferred(RuntimeError):
    pass


@dataclass
class PreparedPipeline:
    offset: float
    run_dir: Path
    video_input: list
    audio_input: list
    delay_procs: list
    audio_map: str = ""
    single_input_av: bool = False
    video_codec: str = ""
    compatible_mux: bool = False
    video_input_label: str = "direct video"
    audio_input_label: str = "direct audio"
    video_snapshot_input: list = field(default_factory=list)
    audio_snapshot_input: list = field(default_factory=list)


@dataclass(frozen=True)
class ClockParse:
    game_time: int
    text: str
    kind: str = "clock"


@dataclass
class AlignmentMonitor:
    video_roi: tuple | None = None
    audio_roi: tuple | None = None
    video_roi_locked: bool = False
    audio_roi_locked: bool = False
    video_clock: str = ""
    audio_clock: str = ""
    state: str = "acquiring"
    message: str = "waiting for timer ROI"
    mismatch_offsets: list = field(default_factory=list)
    mismatch_count: int = 0
    checks: int = 0

    def locked(self):
        return self.video_roi is not None and self.audio_roi is not None

    def snapshot(self):
        return {
            "state": self.state,
            "message": self.message,
            "mismatch_count": self.mismatch_count,
            "checks": self.checks,
            "video_roi": self.video_roi,
            "audio_roi": self.audio_roi,
            "video_roi_locked": self.video_roi_locked,
            "audio_roi_locked": self.audio_roi_locked,
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
        self.status = {
            "running": False,
            "stage": "stopped",
            "active_channel": "",
            "active_url": "",
            "audio_url": "",
            "failure_count": 0,
            "last_error": "",
            "last_resolution": "",
            "last_segment_at": None,
            "started_at": None,
            "offset_seconds": self.profile.get("offset_seconds", 0),
            "auto_align_enabled": parse_bool(self.profile.get("auto_align_enabled", DEFAULT_PROFILE.get("auto_align_enabled", True))),
            "auto_align_msg": "",
            "auto_align_state": "idle",
            "auto_align_monitor": {},
            "last_alignment": None,
            "auto_align_offset_seconds": None,
            "last_snapshot_at": None,
            "schedule": {
                "enabled": parse_bool(self.profile.get("schedule_enabled", DEFAULT_PROFILE.get("schedule_enabled", True))),
                "active": False,
                "active_match": None,
                "next_match": None,
                "last_refresh": None,
                "message": "",
                "manual_override": False,
            },
        }

    def log(self, message):
        line = f"[{now()}] {message}"
        with self.lock:
            self.logs.append(line)
        print(line, flush=True)

    def get_profile(self):
        with self.lock:
            return self.profile.copy()

    def get_public_profile(self):
        with self.lock:
            return _strip_url_fields(self.profile.copy())

    def normalize_profile(self, profile, save=False):
        raw_profile = profile or {}
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
        merged["offset_seconds"] = coerce_float(merged.get("offset_seconds"), DEFAULT_PROFILE["offset_seconds"])
        merged["retry_limit"] = coerce_int(merged.get("retry_limit"), DEFAULT_PROFILE["retry_limit"], minimum=1)
        merged["timeout_seconds"] = coerce_int(merged.get("timeout_seconds"), DEFAULT_PROFILE["timeout_seconds"], minimum=5)
        merged["segment_time"] = effective_segment_time(merged)
        merged["playlist_size"] = coerce_int(merged.get("playlist_size"), DEFAULT_PROFILE["playlist_size"], minimum=3)
        merged["hls_segment_type"] = hls_segment_type(merged)
        merged["auto_align_enabled"] = parse_bool(merged.get("auto_align_enabled", DEFAULT_PROFILE["auto_align_enabled"]))
        merged["auto_align_interval"] = coerce_int(merged.get("auto_align_interval"), DEFAULT_PROFILE["auto_align_interval"], minimum=5)
        merged["auto_align_samples"] = coerce_int(merged.get("auto_align_samples"), DEFAULT_PROFILE["auto_align_samples"], minimum=3)
        merged["auto_align_step"] = coerce_float(merged.get("auto_align_step"), DEFAULT_PROFILE["auto_align_step"], minimum=0.5)
        merged["auto_align_threshold"] = coerce_float(merged.get("auto_align_threshold"), DEFAULT_PROFILE["auto_align_threshold"], minimum=0.1)
        merged["auto_align_max_offset"] = coerce_float(merged.get("auto_align_max_offset"), DEFAULT_PROFILE["auto_align_max_offset"], minimum=1)
        merged["auto_align_relocate_attempts"] = coerce_int(merged.get("auto_align_relocate_attempts"), DEFAULT_PROFILE["auto_align_relocate_attempts"], minimum=0)
        merged["auto_align_stop_after_aligned"] = parse_bool(merged.get("auto_align_stop_after_aligned", DEFAULT_PROFILE["auto_align_stop_after_aligned"]))
        merged["snapshot_interval"] = coerce_int(merged.get("snapshot_interval"), DEFAULT_PROFILE["snapshot_interval"], minimum=10)
        merged["video_roi"] = coerce_text(merged.get("video_roi"), DEFAULT_PROFILE["video_roi"])
        merged["audio_roi"] = coerce_text(merged.get("audio_roi"), DEFAULT_PROFILE["audio_roi"])
        merged["video_roi_presets"] = coerce_text(merged.get("video_roi_presets"), DEFAULT_PROFILE["video_roi_presets"])
        merged["audio_roi_presets"] = coerce_text(merged.get("audio_roi_presets"), DEFAULT_PROFILE["audio_roi_presets"])
        merged["schedule_enabled"] = parse_bool(merged.get("schedule_enabled", DEFAULT_PROFILE["schedule_enabled"]))
        merged["schedule_provider"] = coerce_text(merged.get("schedule_provider"), DEFAULT_PROFILE["schedule_provider"])
        merged["schedule_league"] = coerce_text(merged.get("schedule_league"), DEFAULT_PROFILE["schedule_league"])
        merged["schedule_timezone"] = coerce_text(merged.get("schedule_timezone"), DEFAULT_PROFILE["schedule_timezone"])
        merged["schedule_refresh_hours"] = coerce_int(merged.get("schedule_refresh_hours"), DEFAULT_PROFILE["schedule_refresh_hours"], minimum=1)
        merged["schedule_poll_seconds"] = coerce_int(merged.get("schedule_poll_seconds"), DEFAULT_PROFILE["schedule_poll_seconds"], minimum=30)
        merged["schedule_pre_minutes"] = coerce_int(merged.get("schedule_pre_minutes"), DEFAULT_PROFILE["schedule_pre_minutes"], minimum=0)
        merged["schedule_duration_minutes"] = coerce_int(merged.get("schedule_duration_minutes"), DEFAULT_PROFILE["schedule_duration_minutes"], minimum=90)
        merged["schedule_post_minutes"] = coerce_int(merged.get("schedule_post_minutes"), DEFAULT_PROFILE["schedule_post_minutes"], minimum=0)
        if "auto_align_outside_match" in raw_profile:
            auto_align_outside_match = parse_bool(raw_profile.get("auto_align_outside_match"))
        elif "align_only_during_match" in raw_profile:
            auto_align_outside_match = not parse_bool(raw_profile.get("align_only_during_match"))
        else:
            auto_align_outside_match = parse_bool(DEFAULT_PROFILE["auto_align_outside_match"])
        merged["auto_align_outside_match"] = auto_align_outside_match
        merged["align_only_during_match"] = not auto_align_outside_match
        if save:
            json_save(PROFILE_PATH, _strip_url_fields(merged))
        return merged

    def set_profile(self, profile):
        merged = self.normalize_profile(profile)
        with self.lock:
            self.profile = merged
            self.status["offset_seconds"] = merged["offset_seconds"]
            self.status["auto_align_enabled"] = parse_bool(merged.get("auto_align_enabled", DEFAULT_PROFILE.get("auto_align_enabled", True)))
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
                "enabled": parse_bool(aa.get("auto_align_enabled", DEFAULT_PROFILE.get("auto_align_enabled", True))),
                "active_allowed": self._auto_align_allowed_by_schedule(aa),
                "outside_match": parse_bool(aa.get("auto_align_outside_match", DEFAULT_PROFILE["auto_align_outside_match"])),
                "only_during_match": parse_bool(aa.get("align_only_during_match", DEFAULT_PROFILE["align_only_during_match"])),
                "interval": coerce_int(aa.get("auto_align_interval"), DEFAULT_PROFILE["auto_align_interval"]),
                "samples": coerce_int(aa.get("auto_align_samples"), DEFAULT_PROFILE["auto_align_samples"]),
                "step": coerce_float(aa.get("auto_align_step"), DEFAULT_PROFILE["auto_align_step"]),
                "threshold": coerce_float(aa.get("auto_align_threshold"), DEFAULT_PROFILE["auto_align_threshold"]),
                "max_offset": coerce_float(aa.get("auto_align_max_offset"), DEFAULT_PROFILE["auto_align_max_offset"]),
                "relocate_attempts": coerce_int(aa.get("auto_align_relocate_attempts"), DEFAULT_PROFILE["auto_align_relocate_attempts"], minimum=0),
                "stop_after_aligned": parse_bool(aa.get("auto_align_stop_after_aligned", DEFAULT_PROFILE["auto_align_stop_after_aligned"])),
                "snapshot_interval": coerce_int(aa.get("snapshot_interval"), DEFAULT_PROFILE["snapshot_interval"]),
                "video_roi": coerce_text(aa.get("video_roi"), DEFAULT_PROFILE["video_roi"]),
                "audio_roi": coerce_text(aa.get("audio_roi"), DEFAULT_PROFILE["audio_roi"]),
                "video_roi_presets": aa.get("video_roi_presets", ""),
                "audio_roi_presets": aa.get("audio_roi_presets", ""),
            }
            status["auto_align_state"] = self.status.get("auto_align_state", "idle")
            status["auto_align_monitor"] = dict(self.status.get("auto_align_monitor") or {})
            status["hls_url"] = "/index.m3u8"
            status["emby_url"] = "/emby.m3u"
            playlist = HLS_DIR / "index.m3u8"
            segments = sorted(list(HLS_DIR.glob("live_*.ts")) + list(HLS_DIR.glob("live_*.m4s")))
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
            }
            status["schedule"] = self._schedule_status_snapshot()
            return status

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
            self.status.update({"running": True, "stage": "starting", "started_at": now(), "failure_count": 0, "last_error": ""})
            self.thread = threading.Thread(target=self._run_loop, daemon=True)
            self.thread.start()
        self.log(f"live pipeline requested ({source})")

    def stop(self, source="manual"):
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
        self.stop(source=source)
        thread = None
        with self.lock:
            thread = self.thread
        if thread and thread.is_alive():
            thread.join(timeout=10)
        with self.lock:
            if self.thread and not self.thread.is_alive():
                self.thread = None
        self.start(source=source)

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
        while not self.stop_event.is_set():
            channel_name = channels[index]
            force_refresh = failures >= int(profile.get("retry_limit", 3))
            try:
                video = self.resolver.find_any_sources(video_sources, channel_name, force=force_refresh)
            except Exception as exc:
                self.log(f"video resolve failed for {channel_name}: {exc}")
                index += 1
                failures = 0
                if index >= len(channels):
                    self._fail("all selected video channels failed to resolve")
                    return
                continue

            try:
                if not audio_sources:
                    audio = Channel(name="no audio", url="")
                    self.log("no audio M3U configured; running video-only")
                else:
                    ac = profile.get("audio_channel", "").strip()
                    if not ac:
                        raise RuntimeError(f"audio channel name is empty (audio playlist is set but audio_channel field is blank)")
                    audio = self.resolver.find_any_sources(audio_sources, ac, force=force_refresh)
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
                    self._fail("all selected video channels failed")
                    return
                self.log(f"{channel_name} URL unchanged after {profile.get('retry_limit', 3)} failures; falling back to {channels[index]}")
                continue

            current_url = video.url
            with self.lock:
                self.status.update({
                    "stage": "running",
                    "active_channel": channel_name,
                    "active_url": _redact_url(video.url),
                    "audio_url": _redact_url(audio.url),
                    "failure_count": failures,
                    "last_resolution": f"{channel_name} -> {_redact_url(video.url)}",
                    "offset_seconds": profile.get("offset_seconds", 0),
                })

            reason = self._run_pipeline(video, audio, profile)
            if self.stop_event.is_set():
                break
            if reason.startswith("re-aligned"):
                failures = 0
                continue
            failures += 1
            with self.lock:
                self.status["failure_count"] = failures
                self.status["last_error"] = reason
            self.log(f"pipeline failed for {channel_name}: {reason} (failure {failures}/{profile.get('retry_limit', 3)})")

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
        with self.lock:
            lines = list(self.process_tails.get(proc.pid, []))[-max_lines:]
        if not lines:
            return ""
        return " | recent stderr: " + " || ".join(_redact_url(line) for line in lines)

    def _mux_failure_detail(self, mux, pipeline):
        context = f" | context: {pipeline.video_input_label}; {pipeline.audio_input_label}"
        tail = self._process_tail_summary(mux)
        return context + tail

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
        ]
        if urlsplit(str(url)).path.lower().endswith(".m3u8"):
            args += ["-http_persistent", "0", "-live_start_index", "-1"]
        if headers:
            header_text = "".join(f"{key}: {value}\r\n" for key, value in headers.items())
            args += ["-headers", header_text]
        return args

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
            self.log(f"video codec probe failed for {channel.name}: {exc}; using generic copy path")
            return ""
        codec = result.stdout.strip().splitlines()
        return codec[0].strip().lower() if codec else ""

    def _video_copy_args(self, codec):
        args = ["-c:v", "copy"]
        if codec in ("hevc", "h265"):
            args += ["-tag:v", "hvc1"]
            if strip_dovi_rpu():
                args += ["-bsf:v", "filter_units=remove_types=62"]
        return args

    def _start_delay_recorder(self, channel, playlist, segment, list_size, timeout, kind, video_codec=""):
        url = channel.url
        segment_name = playlist.parent / f"{kind}_%06d.ts"
        if kind == "video":
            stream_args = ["-map", "0:v:0", "-an", *self._video_copy_args(video_codec)]
        else:
            stream_args = ["-map", "0:a:0", "-vn", "-c:a", "copy"]
        header_label = f", headers={','.join(channel.headers.keys())}" if channel.headers else ""
        context = f"source={kind} url={url}{header_label}; output={playlist.name}"
        return self._start_process([
            "ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
            "-rw_timeout", str(timeout * 1_000_000),
            *self._http_input_options(url, channel.headers),
            "-fflags", "+discardcorrupt",
            "-thread_queue_size", "4096", "-i", url,
            *stream_args,
            "-f", "hls", "-hls_time", segment, "-hls_list_size", str(list_size),
            "-hls_flags", "delete_segments+omit_endlist",
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

    def _direct_input(self, url, timeout, headers=None):
        return [
            "-rw_timeout", str(timeout * 1_000_000),
            *self._http_input_options(url, headers),
            "-fflags", "+discardcorrupt",
            "-thread_queue_size", "4096", "-i", url,
        ]

    def _prepare_pipeline(self, video, audio, profile, run_label):
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
        compatible_mux = video_codec not in ("hevc", "h265")
        if video_codec:
            mux_mode = "mpegts compatibility" if compatible_mux else "configured HLS"
            self.log(f"video codec detected: {video_codec}; mux mode: {mux_mode}")
        same_source = (
            bool(audio_url)
            and video_url == audio_url
            and dict(video.headers) == dict(audio.headers)
        )
        if same_source and abs(offset) >= 0.5:
            self.log(f"same video/audio source detected; ignoring stored offset {offset:.3f}s for this run")
            offset = 0.0
        single_input_av = same_source
        video_input = self._direct_input(video_url, timeout, video.headers)
        video_header_label = f", headers={','.join(video.headers.keys())}" if video.headers else ""
        audio_header_label = f", headers={','.join(audio.headers.keys())}" if audio_url and audio.headers else ""
        video_input_label = f"input0=direct video ({video_url}{video_header_label})"
        audio_input = []
        audio_map = ""
        if single_input_av:
            audio_map = "0:a:0"
            audio_input_label = f"input0=audio from same source ({audio_url}{audio_header_label})"
        elif audio_url:
            audio_input = self._direct_input(audio_url, timeout, audio.headers)
            audio_map = "1:a:0"
            audio_input_label = f"input1=direct audio ({audio_url}{audio_header_label})"
        else:
            audio_input_label = "input1=none"
        video_snapshot_input = list(video_input)
        audio_snapshot_input = list(video_input if single_input_av else audio_input)

        try:
            if offset >= 0.5:
                delay_playlist = run_dir / "video_delay.m3u8"
                list_size = max(20, int(offset / segment_seconds) + 20)
                recorder = self._start_delay_recorder(video, delay_playlist, segment, list_size, timeout, "video", video_codec)
                delay_procs.append(recorder)
                self._buffer_delay_input(recorder, delay_playlist, offset, timeout, "buffering video")
                video_input = ["-thread_queue_size", "4096", "-live_start_index", "0", "-i", str(delay_playlist)]
                video_input_label = f"input0=local video delay HLS ({delay_playlist.name}, source={video_url}{video_header_label}, offset +{offset:.3f}s)"
                video_snapshot_input = list(video_input)
            elif offset <= -0.5:
                if not audio_url:
                    raise RuntimeError("negative offset requires an audio source")
                delay_playlist = run_dir / "audio_delay.m3u8"
                list_size = max(20, int(abs(offset) / segment_seconds) + 20)
                recorder = self._start_delay_recorder(audio, delay_playlist, segment, list_size, timeout, "audio")
                delay_procs.append(recorder)
                self._buffer_delay_input(recorder, delay_playlist, offset, timeout, "buffering audio")
                audio_input = ["-thread_queue_size", "4096", "-live_start_index", "0", "-i", str(delay_playlist)]
                audio_map = "1:a:0"
                audio_input_label = f"input1=local audio delay HLS ({delay_playlist.name}, source={audio_url}{audio_header_label}, offset {offset:.3f}s)"
                audio_snapshot_input = list(audio_input)
        except Exception:
            self._stop_processes(delay_procs)
            shutil.rmtree(run_dir, ignore_errors=True)
            raise

        return PreparedPipeline(
            offset=offset,
            run_dir=run_dir,
            video_input=video_input,
            audio_input=audio_input,
            delay_procs=delay_procs,
            audio_map=audio_map,
            single_input_av=single_input_av,
            video_codec=video_codec,
            compatible_mux=compatible_mux,
            video_input_label=video_input_label,
            audio_input_label=audio_input_label,
            video_snapshot_input=video_snapshot_input,
            audio_snapshot_input=audio_snapshot_input,
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
            if output_audio_codec() == "copy":
                cmd += ["-c:a", "copy"]
            else:
                cmd += ["-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2"]
        cmd += [
            "-f", "hls", "-hls_time", segment, "-hls_list_size", str(playlist_size),
            "-hls_delete_threshold", str(max(playlist_size, 10)),
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
            profile = self.profile.copy()
        json_save(PROFILE_PATH, _strip_url_fields(profile))
        json_save(OFFSET_STATE, {
            "offset_seconds": round(offset, 3),
            "updated_at_unix": int(time.time()),
            "source": "auto-align",
        })

    def _handoff_pipeline(self, video, audio, profile, old_pipeline, old_mux, run_label):
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
            prepared = self._prepare_pipeline(video, audio, profile, run_label)
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
        index = HLS_DIR / "index.m3u8"
        tmp = HLS_DIR / ".index.m3u8.tmp"
        tmp.unlink(missing_ok=True)
        try:
            os.symlink(playlist_path.name, tmp)
            os.replace(tmp, index)
        finally:
            tmp.unlink(missing_ok=True)
        self._prune_handoff_hls()

    def _prune_handoff_hls(self):
        playlist = HLS_DIR / "index.m3u8"
        referenced = set(self._playlist_segments(playlist))
        try:
            for line in playlist.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and line.endswith(".mp4"):
                    referenced.add(Path(line).name)
        except FileNotFoundError:
            return

        segments = sorted(
            list(HLS_DIR.glob("live_*.ts")) + list(HLS_DIR.glob("live_*.m4s")),
            key=lambda p: self._hls_segment_number(p) if self._hls_segment_number(p) is not None else -1,
        )
        keep_recent = {p.name for p in segments[-max(30, len(referenced) + 10):]}
        keep = referenced | keep_recent | {"index.m3u8"}
        for item in HLS_DIR.iterdir():
            if item.name in keep or item.name == "snapshots":
                continue
            if item.name.startswith("live_") and item.suffix in (".ts", ".m4s"):
                item.unlink(missing_ok=True)
            elif item.name.startswith("init_") and item.suffix == ".mp4":
                item.unlink(missing_ok=True)
            elif re.match(r"^run_\d+\.m3u8$", item.name):
                item.unlink(missing_ok=True)

    def _set_align_monitor_status(self, monitor, msg=None):
        if msg is not None:
            monitor.message = msg
        with self.lock:
            self.status["auto_align_state"] = monitor.state
            self.status["auto_align_msg"] = monitor.message
            self.status["auto_align_monitor"] = monitor.snapshot()

    def _set_current_snapshot_jobs(self, pipeline, profile):
        jobs = []
        if pipeline.video_snapshot_input:
            jobs.append(("video", pipeline.video_snapshot_input, self.status.get("active_channel") or "active video"))
        if pipeline.audio_snapshot_input:
            jobs.append(("audio", pipeline.audio_snapshot_input, profile.get("audio_channel") or "active audio"))
        with self.lock:
            self.current_snapshot_jobs = jobs

    def _extract_current_frame(self, url, out_path, timeout, headers=None):
        subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-rw_timeout", str(timeout * 1_000_000),
            *self._http_input_options(url, headers),
            "-i", url,
            "-frames:v", "1", "-update", "1", str(out_path),
        ], capture_output=True, timeout=timeout + 5, check=True)

    def _capture_alignment_frames(self, video, audio, tmpdir, timeout):
        video_frame = tmpdir / "align_video.jpg"
        audio_frame = tmpdir / "align_audio.jpg"
        with ThreadPoolExecutor(max_workers=2) as pool:
            futs = [
                pool.submit(self._extract_current_frame, video.url, video_frame, timeout, video.headers),
                pool.submit(self._extract_current_frame, audio.url, audio_frame, timeout, audio.headers),
            ]
            for fut in as_completed(futs):
                fut.result()
        return video_frame, audio_frame

    def _read_locked_clock(self, frame_path, roi):
        if roi is None:
            return None
        parsed = self._ocr_time(frame_path, roi)
        if not parsed:
            return None
        return ClockSample(0.0, parsed[0], parsed[1], roi)

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

    def _find_frame_clock(self, frame_path):
        found = self._find_clock_in_frame(frame_path)
        if not found:
            return None
        return ClockSample(0.0, found[0], found[1], found[2])

    def _find_clock_with_presets(self, frame_path, profile, kind):
        sample = self._read_preset_clock(frame_path, profile, kind)
        if sample:
            return sample, "preset"
        sample = self._find_frame_clock(frame_path)
        if sample:
            return sample, "auto"
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

    def _stable_mismatch_offset(self, offsets):
        if not offsets:
            return None
        ordered = sorted(offsets)
        median = ordered[len(ordered) // 2]
        if max(abs(item - median) for item in ordered) > 1.5:
            return None
        return median

    def _monitor_alignment_once(self, video, audio, profile, monitor):
        if not audio or not audio.url:
            monitor.state = "disabled"
            monitor.message = "video-only; no audio clock to compare"
            return None, monitor.message

        timeout = coerce_int(profile.get("timeout_seconds"), DEFAULT_PROFILE["timeout_seconds"], minimum=5)
        threshold = coerce_float(profile.get("auto_align_threshold"), DEFAULT_PROFILE["auto_align_threshold"], minimum=0.1)
        required_mismatches = coerce_int(profile.get("auto_align_samples"), DEFAULT_PROFILE["auto_align_samples"], minimum=1)
        current = float(profile.get("offset_seconds", 0) or 0)

        with tempfile.TemporaryDirectory(prefix="align_frame_") as tmp:
            tmpdir = Path(tmp)
            try:
                video_frame, audio_frame = self._capture_alignment_frames(video, audio, tmpdir, timeout)
            except Exception as exc:
                monitor.state = "capture_failed"
                monitor.message = f"frame capture failed: {exc}"
                self.log(f"auto-align: {monitor.message}; keeping current offset")
                return None, monitor.message

            monitor.checks += 1
            if not monitor.video_roi:
                sample, source = self._find_clock_with_presets(video_frame, profile, "video")
                if sample:
                    self._lock_monitor_roi(monitor, "video", sample, profile, source)
            if not monitor.audio_roi:
                sample, source = self._find_clock_with_presets(audio_frame, profile, "audio")
                if sample:
                    self._lock_monitor_roi(monitor, "audio", sample, profile, source)

            if not monitor.locked():
                monitor.state = "acquiring"
                missing = []
                if not monitor.video_roi:
                    missing.append("video")
                if not monitor.audio_roi:
                    missing.append("audio")
                monitor.message = "waiting for timer ROI: " + ",".join(missing)
                return None, monitor.message

            video_sample = self._read_locked_clock(video_frame, monitor.video_roi)
            audio_sample = self._read_locked_clock(audio_frame, monitor.audio_roi)
            if video_sample and not monitor.video_roi_locked:
                monitor.video_roi_locked = True
                monitor.video_clock = video_sample.text
                self.log(f"auto-align: locked video ROI from configured {self._format_roi_value(video_sample.roi)} ({video_sample.text})")
            if audio_sample and not monitor.audio_roi_locked:
                monitor.audio_roi_locked = True
                monitor.audio_clock = audio_sample.text
                self.log(f"auto-align: locked audio ROI from configured {self._format_roi_value(audio_sample.roi)} ({audio_sample.text})")
            if not video_sample and not monitor.video_roi_locked:
                monitor.video_roi = None
            if not audio_sample and not monitor.audio_roi_locked:
                monitor.audio_roi = None
            if not monitor.video_roi:
                sample, source = self._find_clock_with_presets(video_frame, profile, "video")
                if sample:
                    video_sample = sample
                    self._lock_monitor_roi(monitor, "video", sample, profile, source)
            if not monitor.audio_roi:
                sample, source = self._find_clock_with_presets(audio_frame, profile, "audio")
                if sample:
                    audio_sample = sample
                    self._lock_monitor_roi(monitor, "audio", sample, profile, source)
            if not monitor.locked():
                monitor.state = "acquiring"
                monitor.message = "configured ROI unreadable; searching timer ROI"
                return None, monitor.message
            if not video_sample or not audio_sample:
                monitor.state = "locked"
                monitor.message = (
                    "timer missing in locked ROI; pausing alignment check "
                    f"({monitor.mismatch_count}/{required_mismatches})"
                )
                return None, monitor.message

            monitor.video_clock = video_sample.text
            monitor.audio_clock = audio_sample.text
            candidate, detail = self._candidate_offset_from_samples(video_sample, audio_sample, profile)
            if candidate is None:
                monitor.state = "locked"
                monitor.message = (
                    detail + "; pausing alignment check "
                    f"({monitor.mismatch_count}/{required_mismatches})"
                )
                return None, monitor.message

            delta = abs(candidate - current)
            if video_sample.game_time == audio_sample.game_time and delta >= threshold:
                monitor.state = "realigning"
                monitor.message = f"matched clocks; new offset {candidate:.3f}s {detail}"
                self.log(f"auto-align: {monitor.message}")
                return candidate, monitor.message

            if delta < threshold:
                monitor.state = "aligned"
                monitor.message = f"stable current={current:.3f}s candidate={candidate:.3f}s {detail}"
                monitor.mismatch_offsets.clear()
                monitor.mismatch_count = 0
                return None, monitor.message

            monitor.mismatch_offsets.append(candidate)
            monitor.mismatch_offsets = monitor.mismatch_offsets[-required_mismatches:]
            monitor.mismatch_count = len(monitor.mismatch_offsets)
            stable = self._stable_mismatch_offset(monitor.mismatch_offsets)
            if monitor.mismatch_count >= required_mismatches and stable is not None:
                monitor.state = "realigning"
                monitor.message = f"{monitor.mismatch_count} mismatches; new offset {stable:.3f}s {detail}"
                self.log(f"auto-align: {monitor.message}")
                return stable, monitor.message

            monitor.state = "mismatch"
            monitor.message = f"mismatch {monitor.mismatch_count}/{required_mismatches}: current={current:.3f}s candidate={candidate:.3f}s {detail}"
            self.log(f"auto-align: {monitor.message}")
            return None, monitor.message

    def _refresh_runtime_auto_align_profile(self, profile):
        with self.lock:
            latest = self.profile.copy()
        for key in RUNTIME_AUTO_ALIGN_KEYS:
            if key in latest:
                profile[key] = latest[key]
        return profile

    def _run_pipeline(self, video, audio, profile):
        video_url = video.url
        audio_url = audio.url if audio else ""
        self._stop_processes()
        self._clear_hls()
        WORK_DIR.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(WORK_DIR, ignore_errors=True)
        WORK_DIR.mkdir(parents=True, exist_ok=True)

        timeout = coerce_int(profile.get("timeout_seconds"), DEFAULT_PROFILE["timeout_seconds"], minimum=5)
        run_id = 0
        try:
            current_pipeline = self._prepare_pipeline(video, audio, profile, f"run_{run_id:03d}")
        except Exception as exc:
            return str(exc)
        self._set_current_snapshot_jobs(current_pipeline, profile)
        mux = self._start_mux(current_pipeline, profile, start_number=0)

        first_segment_deadline = time.time() + timeout
        last_align_check = 0.0
        last_snapshot_check = 0.0
        auto_align_profile = profile.copy()
        freeze_after_aligned = parse_bool(auto_align_profile.get("auto_align_stop_after_aligned", DEFAULT_PROFILE.get("auto_align_stop_after_aligned", False)))
        alignment_frozen = False
        align_monitor = AlignmentMonitor()
        try:
            align_monitor.video_roi = parse_roi(auto_align_profile.get("video_roi", DEFAULT_PROFILE["video_roi"]))
            align_monitor.audio_roi = parse_roi(auto_align_profile.get("audio_roi", DEFAULT_PROFILE["audio_roi"]))
            align_monitor.state = "acquiring"
            align_monitor.message = "trying configured timer ROI"
        except ValueError:
            align_monitor.state = "acquiring"
            align_monitor.message = "waiting for timer ROI"
        with self.lock:
            self.status["auto_align_state"] = align_monitor.state
            self.status["auto_align_msg"] = align_monitor.message
            self.status["auto_align_monitor"] = align_monitor.snapshot()
        while not self.stop_event.is_set():
            auto_align_profile = self._refresh_runtime_auto_align_profile(auto_align_profile)
            freeze_after_aligned = parse_bool(auto_align_profile.get("auto_align_stop_after_aligned", DEFAULT_PROFILE.get("auto_align_stop_after_aligned", False)))
            if mux.poll() is not None:
                return f"ffmpeg exited with code {mux.returncode}{self._mux_failure_detail(mux, current_pipeline)}"
            mtime = self._latest_hls_mtime()
            if mtime:
                with self.lock:
                    self.status["last_segment_at"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime))
                    self.status["stage"] = "running"
                if time.time() - mtime > timeout:
                    age = time.time() - mtime
                    return f"no new HLS segment for {timeout}s (last segment {age:.1f}s ago){self._mux_failure_detail(mux, current_pipeline)}"
            elif time.time() > first_segment_deadline:
                return f"no HLS segment created within {timeout}s{self._mux_failure_detail(mux, current_pipeline)}"
            snapshot_interval = coerce_float(auto_align_profile.get("snapshot_interval"), DEFAULT_PROFILE["snapshot_interval"])
            align_allowed_now = self._auto_align_allowed_by_schedule(auto_align_profile)
            if not align_allowed_now and align_monitor.state != "disabled":
                align_monitor.state = "disabled"
                self._set_align_monitor_status(align_monitor, "非比赛时间：直播继续，暂停自动截图和对齐")
            if not alignment_frozen and align_allowed_now and time.time() - last_snapshot_check >= snapshot_interval and mtime:
                last_snapshot_check = time.time()
                self._capture_runtime_snapshots(video, audio, auto_align_profile)
            # Periodic auto-align
            if not alignment_frozen and align_allowed_now and parse_bool(auto_align_profile.get("auto_align_enabled", DEFAULT_PROFILE.get("auto_align_enabled", True))):
                a_interval = coerce_float(auto_align_profile.get("auto_align_interval"), DEFAULT_PROFILE["auto_align_interval"])
                if time.time() - last_align_check >= a_interval and mtime:
                    last_align_check = time.time()
                    new_off, a_msg = self._monitor_alignment_once(video, audio, auto_align_profile, align_monitor)
                    if new_off is not None:
                        next_profile = auto_align_profile.copy()
                        next_profile["offset_seconds"] = new_off
                        try:
                            run_id += 1
                            current_pipeline, mux = self._handoff_pipeline(
                                video, audio, next_profile,
                                current_pipeline, mux, f"run_{run_id:03d}"
                            )
                            auto_align_profile = next_profile
                            self._set_current_snapshot_jobs(current_pipeline, auto_align_profile)
                            self._save_auto_offset(new_off)
                            align_monitor.mismatch_offsets.clear()
                            align_monitor.mismatch_count = 0
                            align_monitor.state = "aligned"
                            first_segment_deadline = time.time() + timeout
                            self._set_align_monitor_status(align_monitor, f"{a_msg}; handoff complete")
                            if freeze_after_aligned:
                                alignment_frozen = True
                                align_monitor.state = "aligned"
                                self._set_align_monitor_status(align_monitor, f"{a_msg}; handoff complete; 已暂停自动截图和检查")
                                self.log("auto-align: aligned once; paused automatic snapshots and checks")
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
                        if freeze_after_aligned and align_monitor.state == "aligned":
                            alignment_frozen = True
                            self._set_align_monitor_status(align_monitor, f"{a_msg}; 已暂停自动截图和检查")
                            self.log("auto-align: already aligned; paused automatic snapshots and checks")
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
        if parse_bool(profile.get("auto_align_outside_match", DEFAULT_PROFILE.get("auto_align_outside_match", False))):
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

    def _record_alignment_clip(self, url, out_path, duration, timeout, headers=None):
        attempts = [
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-rw_timeout", str(timeout * 1_000_000),
                *self._http_input_options(url, headers),
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
        cleaned = re.sub(r"\s+", "", text or "")
        cleaned = cleaned.replace("O", "0").replace("o", "0").replace("＋", "+")
        m = STOPPAGE_RE.search(cleaned)
        if m:
            parsed = self._combine_stoppage_parts(m.group(1), "00", m.group(2), m.group(3), m.group(0))
            if parsed:
                return parsed
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

    def _preprocess_ocr_image(self, img, scale=6):
        if img is None or img.size == 0:
            return None
        resized = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        gray = cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY)
        blur = cv2.GaussianBlur(gray, (3, 3), 0)
        _, thr = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        return cv2.copyMakeBorder(thr, 30, 30, 30, 30, cv2.BORDER_CONSTANT, value=255)

    def _run_tesseract(self, image, *, psm="7", tsv=False, timeout=30):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            ocr_input = f.name
        try:
            cv2.imwrite(ocr_input, image)
            cmd = [
                "tesseract", ocr_input, "stdout", "--psm", psm,
                "-c", "tessedit_char_whitelist=0123456789:+."
            ]
            if tsv:
                cmd.append("tsv")
            proc_env = os.environ.copy()
            proc_env.setdefault("OMP_THREAD_LIMIT", "1")
            proc_env.setdefault("OMP_NUM_THREADS", "1")
            with self.ocr_lock:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=proc_env)
            return proc.stdout
        finally:
            try:
                os.unlink(ocr_input)
            except OSError:
                pass

    def _ocr_time(self, frame_path, roi, scale=6):
        img = cv2.imread(str(frame_path))
        if img is None:
            return None
        h, w = img.shape[:2]
        x, y, rw, rh = roi
        crop = img[int(y*h):int((y+rh)*h), int(x*w):int((x+rw)*w)]
        if crop.size == 0:
            return None
        processed = self._preprocess_ocr_image(crop, scale=scale)
        if processed is None:
            return None
        parsed = self._parse_clock_text(self._run_tesseract(processed, psm="7").strip())
        if not parsed or parsed.kind != "stoppage":
            stoppage = None
            for psm in ("6", "11", "12"):
                rows = self._parse_tsv_rows(self._run_tesseract(processed, psm=psm, tsv=True))
                if not rows:
                    continue
                candidates = self._stoppage_line_candidates(rows)
                if not candidates:
                    continue
                candidates.sort(key=lambda item: item[0], reverse=True)
                stoppage = candidates[0][1]
                break
            if stoppage:
                parsed = stoppage
            elif not parsed:
                parsed = self._parse_clock_text(self._run_tesseract(processed, psm="6").strip())
            if not parsed:
                return None
        return parsed.game_time, parsed.text

    def _roi_crop(self, frame_path, roi):
        img = cv2.imread(str(frame_path))
        if img is None:
            return None
        h, w = img.shape[:2]
        x, y, rw, rh = roi
        crop = img[int(y*h):int((y+rh)*h), int(x*w):int((x+rw)*w)]
        return crop if crop.size else None

    def _parse_tsv_rows(self, tsv_text):
        rows = []
        lines = [line for line in (tsv_text or "").splitlines() if line.strip()]
        if len(lines) < 2:
            return rows
        header = lines[0].split("\t")
        for line in lines[1:]:
            parts = line.split("\t")
            if len(parts) < len(header):
                parts += [""] * (len(header) - len(parts))
            row = dict(zip(header, parts))
            text = row.get("text", "").strip()
            if not text:
                continue
            try:
                conf = float(row.get("conf", "-1"))
                left = int(float(row.get("left", "0")))
                top = int(float(row.get("top", "0")))
                width = int(float(row.get("width", "0")))
                height = int(float(row.get("height", "0")))
            except ValueError:
                continue
            if width <= 0 or height <= 0:
                continue
            rows.append({"text": text, "conf": conf, "left": left, "top": top, "width": width, "height": height})
        return rows

    def _row_box(self, boxes):
        left = min(b["left"] for b in boxes)
        top = min(b["top"] for b in boxes)
        right = max(b["left"] + b["width"] for b in boxes)
        bottom = max(b["top"] + b["height"] for b in boxes)
        return {
            "left": left,
            "top": top,
            "width": right - left,
            "height": bottom - top,
            "right": right,
            "bottom": bottom,
            "text": "".join(b["text"] for b in boxes),
            "conf": sum(max(0, b["conf"]) for b in boxes) / max(1, len(boxes)),
        }

    def _token_box(self, row):
        box = row.copy()
        box["right"] = box["left"] + box["width"]
        box["bottom"] = box["top"] + box["height"]
        return box

    def _group_tsv_lines(self, rows):
        lines = []
        for row in sorted(rows, key=lambda r: (r["top"], r["left"])):
            center = row["top"] + row["height"] / 2
            placed = False
            for line in lines:
                avg_center = sum(r["top"] + r["height"] / 2 for r in line) / len(line)
                avg_height = sum(r["height"] for r in line) / len(line)
                if abs(center - avg_center) <= max(8, avg_height * 0.65):
                    line.append(row)
                    placed = True
                    break
            if not placed:
                lines.append([row])
        boxes = []
        for line in lines:
            boxes.append(self._row_box(sorted(line, key=lambda r: r["left"])))
        return sorted(boxes, key=lambda b: (b["top"], b["left"]))

    def _stoppage_line_candidates(self, rows):
        lines = self._group_tsv_lines(rows)
        boxes = [self._token_box(row) for row in rows] + lines
        bases = []
        additions = []
        elapsed_additions = []
        for box in boxes:
            base = self._parse_stoppage_base_text(box["text"])
            if base:
                bases.append((box, base))
            added = self._parse_added_time_text(box["text"])
            if added:
                additions.append((box, added))
            elapsed = self._parse_elapsed_added_time_text(box["text"])
            if elapsed:
                elapsed_additions.append((box, elapsed))
        candidates = []
        for base_line, base in bases:
            base_center = base_line["left"] + base_line["width"] / 2
            for added_line, added in elapsed_additions:
                if added_line["top"] <= base_line["top"]:
                    continue
                vertical_gap = added_line["top"] - base_line["bottom"]
                max_gap = max(base_line["height"], added_line["height"]) * 2.8
                if vertical_gap < -base_line["height"] * 0.25 or vertical_gap > max_gap:
                    continue
                added_center = added_line["left"] + added_line["width"] / 2
                center_gap = abs(added_center - base_center)
                max_center_gap = max(base_line["width"], added_line["width"]) * 1.4
                overlap = min(base_line["right"], added_line["right"]) - max(base_line["left"], added_line["left"])
                if overlap < 0 and center_gap > max_center_gap:
                    continue
                parsed = self._combine_elapsed_stoppage_parts(
                    base["minute"],
                    base["second"],
                    added["minute"],
                    added["second"],
                    f"{base['text']}+{added['text']}",
                )
                if not parsed:
                    continue
                left = min(base_line["left"], added_line["left"])
                top = min(base_line["top"], added_line["top"])
                right = max(base_line["right"], added_line["right"])
                bottom = max(base_line["bottom"], added_line["bottom"])
                conf = (base_line["conf"] + added_line["conf"]) / 2
                candidates.append((conf + 30, parsed, left, top, right - left, bottom - top))
            for added_line, added in additions:
                if added_line["top"] <= base_line["top"]:
                    continue
                vertical_gap = added_line["top"] - base_line["bottom"]
                max_gap = max(base_line["height"], added_line["height"]) * 2.8
                if vertical_gap < -base_line["height"] * 0.25 or vertical_gap > max_gap:
                    continue
                added_center = added_line["left"] + added_line["width"] / 2
                center_gap = abs(added_center - base_center)
                max_center_gap = max(base_line["width"], added_line["width"]) * 0.9
                overlap = min(base_line["right"], added_line["right"]) - max(base_line["left"], added_line["left"])
                if overlap < 0 and center_gap > max_center_gap:
                    continue
                parsed = self._combine_stoppage_parts(
                    base["minute"],
                    base["second"],
                    added["minute"],
                    added["second"],
                    f"{base['text']}{added['text']}",
                )
                if not parsed:
                    continue
                left = min(base_line["left"], added_line["left"])
                top = min(base_line["top"], added_line["top"])
                right = max(base_line["right"], added_line["right"])
                bottom = max(base_line["bottom"], added_line["bottom"])
                conf = (base_line["conf"] + added_line["conf"]) / 2
                candidates.append((conf, parsed, left, top, right - left, bottom - top))
        return candidates

    def _roi_from_box(self, left, top, width, height, frame_w, frame_h, pad=0.35):
        x0 = max(0, left - width * pad)
        y0 = max(0, top - height * pad)
        x1 = min(frame_w, left + width * (1 + pad))
        y1 = min(frame_h, top + height * (1 + pad))
        if x1 <= x0 or y1 <= y0:
            return None
        return (x0 / frame_w, y0 / frame_h, (x1 - x0) / frame_w, (y1 - y0) / frame_h)

    def _find_clock_in_frame(self, frame_path):
        img = cv2.imread(str(frame_path))
        if img is None:
            return None
        h, w = img.shape[:2]
        # Match clocks are expected near broadcast scoreboards; scanning only the
        # top third avoids replay graphics and lowers OCR CPU.
        scan_h = max(1, h // 3)
        scan = img[:scan_h, :]
        scale = min(1.0, 1280.0 / max(w, scan_h))
        work = cv2.resize(scan, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale < 1 else scan
        processed = self._preprocess_ocr_image(work, scale=2)
        if processed is None:
            return None
        rows_by_psm = []
        for psm in ("6", "11", "12"):
            rows = self._parse_tsv_rows(self._run_tesseract(processed, psm=psm, tsv=True))
            if rows:
                rows_by_psm.append(rows)
        if not rows_by_psm:
            return None
        candidates = []
        src_scale = scale * 2
        combined_rows = [row for rows in rows_by_psm for row in rows]
        for conf, parsed, left, top, width, height in self._stoppage_line_candidates(combined_rows):
            roi = self._roi_from_box(
                (left - 30) / src_scale,
                (top - 30) / src_scale,
                width / src_scale,
                height / src_scale,
                w, h,
            )
            if roi:
                candidates.append((conf + 20, parsed, roi))
        for rows in rows_by_psm:
            for i, row in enumerate(rows):
                texts = []
                boxes = []
                for item in rows[i:i + 4]:
                    texts.append(item["text"])
                    boxes.append(item)
                    if "+" in "".join(texts):
                        continue
                    parsed = self._parse_clock_text("".join(texts))
                    if not parsed:
                        continue
                    conf = sum(max(0, b["conf"]) for b in boxes) / max(1, len(boxes))
                    left = min(b["left"] for b in boxes)
                    top = min(b["top"] for b in boxes)
                    right = max(b["left"] + b["width"] for b in boxes)
                    bottom = max(b["top"] + b["height"] for b in boxes)
                    width = right - left
                    height = bottom - top
                    if width <= 0 or height <= 0:
                        continue
                    # Preprocessing adds a 30px border after a 2x OCR scale.
                    roi = self._roi_from_box(
                        (left - 30) / src_scale,
                        (top - 30) / src_scale,
                        width / src_scale,
                        height / src_scale,
                        w, h
                    )
                    if roi:
                        candidates.append((conf, parsed, roi))
        if not candidates:
            return None
        candidates.sort(key=lambda item: (item[1].kind == "stoppage", item[0]), reverse=True)
        parsed = candidates[0][1]
        return parsed.game_time, parsed.text, candidates[0][2]

    def _collect_clock_samples(self, url, roi, start, end, step, workdir, label, timeout, *, auto_find=False, headers=None):
        samples = []
        tasks = {}
        with ThreadPoolExecutor(max_workers=4) as pool:
            t = start
            while t <= end + 1e-6:
                out = workdir / f"{label}_{t:.3f}.jpg"
                tasks[pool.submit(self._extract_frame, url, t, out, timeout, headers)] = (t, out)
                t += step
            for fut in as_completed(tasks):
                t, out = tasks[fut]
                try:
                    fut.result()
                    parsed = self._ocr_time(out, roi)
                    if parsed:
                        samples.append(ClockSample(t, parsed[0], parsed[1], roi))
                    elif auto_find:
                        found = self._find_clock_in_frame(out)
                        if found:
                            samples.append(ClockSample(t, found[0], found[1], found[2]))
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
        for prev, cur in zip(ordered, ordered[1:]):
            media_delta = cur.media_time - prev.media_time
            clock_delta = cur.game_time - prev.game_time
            if clock_delta < -1:
                return False
            if abs(clock_delta - media_delta) <= tolerance:
                good_pairs += 1
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
        updates = {}
        if monitor.video_roi_locked and monitor.video_roi:
            formatted = self._format_roi_value(monitor.video_roi)
            if formatted != str(profile.get("video_roi", "")):
                updates["video_roi"] = formatted
        if monitor.audio_roi_locked and monitor.audio_roi:
            formatted = self._format_roi_value(monitor.audio_roi)
            if formatted != str(profile.get("audio_roi", "")):
                updates["audio_roi"] = formatted
        if not updates:
            return
        with self.lock:
            self.profile.update(updates)
            profile.update(updates)
            saved = self.profile.copy()
        json_save(PROFILE_PATH, _strip_url_fields(saved))
        self.log("auto-align: saved locked ROI " + " ".join(f"{k}={v}" for k, v in updates.items()))

    def _estimate_clock_offset(self, video_samples, audio_samples, profile):
        requested_samples = coerce_int(profile.get("auto_align_samples"), DEFAULT_PROFILE["auto_align_samples"])
        min_samples = max(3, min(requested_samples, math.ceil(requested_samples * 0.6)))
        step = coerce_float(profile.get("auto_align_step"), DEFAULT_PROFILE["auto_align_step"], minimum=0.5)
        motion_threshold = self._clock_motion_threshold((requested_samples - 1) * step)
        max_offset = coerce_float(profile.get("auto_align_max_offset"), DEFAULT_PROFILE["auto_align_max_offset"])
        cluster_window = 2.5
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
        best_cluster = []
        for i, c in enumerate(candidates):
            cluster = [cc for cc in candidates[i:] if cc.offset - c.offset <= cluster_window]
            if len(cluster) > len(best_cluster):
                best_cluster = cluster
        if len(best_cluster) < min_samples:
            return None, f"cluster {len(best_cluster)} < {min_samples}"
        offsets = sorted(c.offset for c in best_cluster)
        median = offsets[len(offsets)//2]
        return median, f"aligned ({len(best_cluster)} matches)"

    def _run_auto_align(self, video_url, audio_url, profile, *, allow_relocate=False):
        video = video_url if isinstance(video_url, Channel) else Channel(name="video", url=video_url)
        audio = audio_url if isinstance(audio_url, Channel) else Channel(name="audio", url=audio_url)
        enabled = parse_bool(profile.get("auto_align_enabled", DEFAULT_PROFILE.get("auto_align_enabled", True)))
        if not enabled:
            return None, "disabled"
        sample_count = coerce_int(profile.get("auto_align_samples"), DEFAULT_PROFILE["auto_align_samples"])
        min_samples = max(3, min(sample_count, math.ceil(sample_count * 0.6)))
        step = coerce_float(profile.get("auto_align_step"), DEFAULT_PROFILE["auto_align_step"])
        timeout = coerce_int(profile.get("timeout_seconds"), DEFAULT_PROFILE["timeout_seconds"])
        threshold = coerce_float(profile.get("auto_align_threshold"), DEFAULT_PROFILE["auto_align_threshold"])
        try:
            video_roi = parse_roi(profile.get("video_roi", "0.050,0.050,0.070,0.050"))
            audio_roi = parse_roi(profile.get("audio_roi", "0.885,0.085,0.075,0.060"))
        except ValueError as exc:
            return None, f"bad roi: {exc}"

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
        roi_key = "audio_roi" if kind == "audio" else "video_roi"
        return parse_roi(profile.get(roi_key, DEFAULT_PROFILE[roi_key]))

    def _save_snapshot_from_frame(self, frame_path, kind, profile, source_name):
        with self.snapshot_file_lock:
            SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
            roi = self._snapshot_roi_for_kind(profile, kind)
            parsed = self._ocr_time(frame_path, roi)
            found_roi = None
            if not parsed:
                preset_sample = self._read_preset_clock(frame_path, profile, kind)
                if preset_sample:
                    parsed = (preset_sample.game_time, preset_sample.text)
                    found_roi = preset_sample.roi
            if not parsed:
                found = self._find_clock_in_frame(frame_path)
                if found and self._roi_center_distance(found[2], roi) <= 0.18:
                    parsed = (found[0], found[1])
                    found_roi = found[2]
            suffix = "timer" if parsed else "full"
            out = SNAPSHOT_DIR / f"{kind}_snapshot.jpg"
            tmp_out = SNAPSHOT_DIR / f".{kind}_snapshot.tmp.jpg"
            if parsed:
                crop = self._roi_crop(frame_path, found_roi or roi)
                if crop is not None:
                    cv2.imwrite(str(tmp_out), crop, [int(cv2.IMWRITE_JPEG_QUALITY), 95])
                else:
                    shutil.copyfile(frame_path, tmp_out)
                    suffix = "full"
            else:
                shutil.copyfile(frame_path, tmp_out)

            os.replace(tmp_out, out)
            self._prune_snapshots()
            with self.lock:
                self.status["last_snapshot_at"] = now()
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

    def _capture_runtime_snapshots(self, video, audio, profile):
        if not self.snapshot_lock.acquire(blocking=False):
            return

        def worker():
            try:
                with self.lock:
                    jobs = list(self.current_snapshot_jobs)
                if not jobs:
                    timeout = coerce_int(profile.get("timeout_seconds"), DEFAULT_PROFILE["timeout_seconds"], minimum=5)
                    jobs = [("video", self._direct_input(video.url, timeout, video.headers), self.status.get("active_channel") or "active video")]
                    if audio and audio.url:
                        jobs.append(("audio", self._direct_input(audio.url, timeout, audio.headers), profile.get("audio_channel") or "active audio"))
                _, errors = self._capture_snapshot_jobs(jobs, profile)
                if errors:
                    self.log("auto snapshot failed: " + "; ".join(f"{kind}: {msg}" for kind, msg in errors.items()))
            finally:
                self.snapshot_lock.release()

        threading.Thread(target=worker, daemon=True).start()

    def _capture_snapshot_jobs(self, jobs, profile):
        jobs = [job for job in jobs if job[1]]
        frames = {}
        results = {}
        errors = {}
        if not jobs:
            return results, {"snapshot": "no snapshot URLs available"}
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
        try:
            for kind in ("video", "audio"):
                if kind not in frames:
                    continue
                frame_path, source_name = frames[kind]
                try:
                    results[kind] = self._save_snapshot_from_frame(frame_path, kind, profile, source_name)
                except Exception as exc:
                    errors[kind] = str(exc)
        finally:
            for frame_path, _source_name in frames.values():
                frame_path.unlink(missing_ok=True)
        return results, errors

    def _prune_snapshots(self):
        keep = {"video_snapshot.jpg", "audio_snapshot.jpg"}
        for item in SNAPSHOT_DIR.glob("*.jpg"):
            if item.name not in keep:
                item.unlink(missing_ok=True)

    def _resolve_snapshot_channel(self, kind, profile, force=True):
        if kind == "audio":
            sources = m3u_sources(profile.get("audio_playlist", ""), profile.get("audio_local_m3u", ""), "本地音频 M3U")
            if not sources:
                raise RuntimeError("no audio M3U configured")
            return self.resolver.find_any_sources(sources, profile["audio_channel"], force=force)

        with self.lock:
            active = self.status.get("active_channel") or profile.get("video_primary")
        sources = m3u_sources(profile.get("video_playlist", ""), profile.get("video_local_m3u", ""), "本地视频 M3U")
        if not sources:
            raise RuntimeError("no video M3U configured")
        return self.resolver.find_any_sources(sources, active, force=force)

    def capture_snapshots(self):
        profile = self.get_profile()
        if not self.snapshot_lock.acquire(blocking=False):
            raise RuntimeError("snapshot capture already running")
        try:
            with self.lock:
                jobs = list(self.current_snapshot_jobs)
            if not jobs:
                jobs = []
                timeout = coerce_int(profile.get("timeout_seconds"), DEFAULT_PROFILE["timeout_seconds"], minimum=5)
                for kind in ("video", "audio"):
                    channel = self._resolve_snapshot_channel(kind, profile, force=True)
                    jobs.append((kind, self._direct_input(channel.url, timeout, channel.headers), channel.name))
            results, errors = self._capture_snapshot_jobs(jobs, profile)
        finally:
            self.snapshot_lock.release()
        if errors and not results:
            raise RuntimeError("; ".join(f"{kind}: {msg}" for kind, msg in errors.items()))
        return {
            "snapshots": [results[kind] for kind in ("video", "audio") if kind in results],
            "errors": errors,
        }

    def capture_snapshot(self, kind):
        profile = self.get_profile()
        if not self.snapshot_lock.acquire(blocking=False):
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
            clear_directory_contents(STATE_DIR, keep={"profile.json", OFFSET_STATE.name})
            SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
            json_save(PROFILE_PATH, _strip_url_fields(self.get_profile()))
            self.log("cleared runtime state")
            return {"ok": True, "target": target}
        raise RuntimeError("target must be hls or state")


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
        if path == "/api/roi":
            return self.send_json(MANAGER.roi)
        if path == "/api/snapshots":
            SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
            shots = []
            for kind in ("video", "audio"):
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
        if path == "/index.m3u8" or path.endswith(".ts") or path.endswith(".m4s") or re.match(r"^/init_[A-Za-z0-9_.-]+\.mp4$", path):
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
            if path == "/api/clear":
                return self.send_json(MANAGER.clear_runtime(data.get("target", "")))
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
