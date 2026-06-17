from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


DEFAULT_STATE_DIR = "/state"
DEFAULT_HLS_DIR = "/hls"
URL_SAVE_KEYS = {
    "video_url",
    "audio_url",
    "public_base_url",
}


def state_dir() -> Path:
    return Path(os.environ.get("LIVE_SYNC_STATE_DIR", DEFAULT_STATE_DIR))


def hls_dir() -> Path:
    return Path(os.environ.get("LIVE_SYNC_HLS_DIR", DEFAULT_HLS_DIR))


class JsonStore:
    def __init__(self, root: Path | None = None):
        self.root = root or state_dir()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "profiles").mkdir(parents=True, exist_ok=True)
        (self.root / "snapshots").mkdir(parents=True, exist_ok=True)

    def profile_path(self, profile_id: str) -> Path:
        clean = safe_id(profile_id)
        return self.root / "profiles" / f"{clean}.json"

    def list_profiles(self) -> list[dict[str, Any]]:
        profiles = []
        for path in sorted((self.root / "profiles").glob("*.json")):
            profiles.append(self.read_json(path, default={}))
        return profiles

    def load_profile(self, profile_id: str) -> dict[str, Any]:
        path = self.profile_path(profile_id)
        if not path.exists():
            raise FileNotFoundError(profile_id)
        return self.read_json(path, default={})

    def save_profile(self, profile: dict[str, Any]) -> dict[str, Any]:
        profile = dict(profile)
        profile_id = safe_id(str(profile.get("id") or "default"))
        now = int(time.time())
        profile["id"] = profile_id
        profile.setdefault("created_at_unix", now)
        profile["updated_at_unix"] = now
        self.write_json(self.profile_path(profile_id), strip_url_fields(profile))
        return profile

    def delete_profile(self, profile_id: str) -> None:
        self.profile_path(profile_id).unlink()

    def load_offset(self, profile_id: str = "default") -> float | None:
        data = self.read_json(self.root / "offsets.json", default={})
        value = data.get(safe_id(profile_id))
        if value is None:
            value = data.get("default")
        try:
            return None if value is None else float(value)
        except (TypeError, ValueError):
            return None

    def save_offset(self, profile_id: str, offset_seconds: float) -> None:
        path = self.root / "offsets.json"
        data = self.read_json(path, default={})
        data[safe_id(profile_id)] = round(float(offset_seconds), 3)
        self.write_json(path, data)

    def load_roi(self, profile_id: str = "default") -> dict[str, Any]:
        data = self.read_json(self.root / "roi.json", default={})
        roi = data.get(safe_id(profile_id)) or data.get("default") or {}
        return roi if isinstance(roi, dict) else {}

    def save_roi(self, profile_id: str, roi: dict[str, Any]) -> None:
        path = self.root / "roi.json"
        data = self.read_json(path, default={})
        data[safe_id(profile_id)] = roi
        self.write_json(path, data)

    def read_json(self, path: Path, default: Any) -> Any:
        try:
            with path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return default
        except (OSError, json.JSONDecodeError):
            return default

    def write_json(self, path: Path, data: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(f"{path.suffix}.tmp")
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=True, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp_path, path)


def safe_id(value: str) -> str:
    clean = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in value.strip())
    clean = clean.strip("-_")
    if not clean:
        raise ValueError("id is required")
    return clean[:80]


def strip_url_fields(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: strip_url_fields(item)
            for key, item in value.items()
            if key not in URL_SAVE_KEYS
        }
    if isinstance(value, list):
        return [strip_url_fields(item) for item in value]
    return value
