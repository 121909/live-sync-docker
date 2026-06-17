from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable
from urllib.parse import urljoin
from urllib.request import Request, urlopen


USER_AGENT = "live-sync-control-plane/1.0"
ATTRIBUTE_RE = re.compile(r'([A-Za-z0-9_-]+)="([^"]*)"')


@dataclass(frozen=True)
class M3UEntry:
    name: str
    url: str
    attrs: dict[str, str] = field(default_factory=dict)
    raw_extinf: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "url": self.url,
            "attrs": self.attrs,
        }


def normalize_name(value: str) -> str:
    return " ".join(value.strip().casefold().split())


def fetch_text(url: str, timeout: float = 12.0) -> str:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def parse_m3u(text: str, base_url: str | None = None) -> list[M3UEntry]:
    entries: list[M3UEntry] = []
    pending_extinf = ""
    pending_attrs: dict[str, str] = {}
    pending_name = ""

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#EXTINF:"):
            pending_extinf = line
            pending_attrs = dict(ATTRIBUTE_RE.findall(line))
            pending_name = _name_from_extinf(line, pending_attrs)
            continue
        if line.startswith("#"):
            continue

        stream_url = urljoin(base_url, line) if base_url else line
        name = pending_name or stream_url
        entries.append(
            M3UEntry(
                name=name,
                url=stream_url,
                attrs=pending_attrs,
                raw_extinf=pending_extinf,
            )
        )
        pending_extinf = ""
        pending_attrs = {}
        pending_name = ""

    return entries


def _name_from_extinf(line: str, attrs: dict[str, str]) -> str:
    if "tvg-name" in attrs and attrs["tvg-name"].strip():
        return attrs["tvg-name"].strip()
    if "," in line:
        return line.rsplit(",", 1)[1].strip()
    return ""


def filter_entries(entries: Iterable[M3UEntry], query: str = "", limit: int = 100) -> list[M3UEntry]:
    normalized_query = normalize_name(query)
    matched: list[M3UEntry] = []
    for entry in entries:
        haystack = " ".join(
            [
                entry.name,
                entry.attrs.get("tvg-name", ""),
                entry.attrs.get("tvg-id", ""),
                entry.attrs.get("group-title", ""),
                entry.url,
            ]
        )
        if not normalized_query or normalized_query in normalize_name(haystack):
            matched.append(entry)
        if len(matched) >= limit:
            break
    return matched


class M3UResolver:
    def __init__(self, timeout: float = 12.0):
        self.timeout = timeout

    def fetch(self, playlist_url: str) -> list[M3UEntry]:
        return parse_m3u(fetch_text(playlist_url, self.timeout), playlist_url)

    def resolve(self, playlist_url: str, channel_name: str) -> M3UEntry:
        entries = self.fetch(playlist_url)
        return resolve_entry(entries, channel_name)


def resolve_entry(entries: Iterable[M3UEntry], channel_name: str) -> M3UEntry:
    wanted = normalize_name(channel_name)
    fallback_contains: M3UEntry | None = None
    for entry in entries:
        names = [
            entry.name,
            entry.attrs.get("tvg-name", ""),
            entry.attrs.get("tvg-id", ""),
        ]
        normalized_names = [normalize_name(name) for name in names if name]
        if wanted in normalized_names:
            return entry
        if fallback_contains is None and any(wanted in name for name in normalized_names):
            fallback_contains = entry
    if fallback_contains:
        return fallback_contains
    raise KeyError(f"channel not found in playlist: {channel_name}")
