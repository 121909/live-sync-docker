#!/usr/bin/env python3
import functools
import http.server
import json
import mimetypes
import os
import re
import shutil
import socketserver
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


MAX_BODY_BYTES = 1024 * 1024
MAX_PLAYLIST_BYTES = 8 * 1024 * 1024
SCREENSHOT_LIMIT = 24


def now_unix():
    return int(time.time())


def utc_stamp(ts=None):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts or time.time()))


def default_manager_state():
    return {
        "playlists": [],
        "profile": {
            "videoPrimary": None,
            "videoFallbacks": [],
            "audio": None,
            "manualOffsetSeconds": None,
            "autoAdjustWhenNoTimer": False,
        },
        "resolution": {
            "activeSource": "environment",
            "lastOutcome": "Using VIDEO_URL and AUDIO_URL from environment until a profile is saved.",
            "lastResolvedAt": None,
            "failureCount": 0,
            "currentVideoUrl": os.environ.get("VIDEO_URL", ""),
            "currentAudioUrl": os.environ.get("AUDIO_URL", ""),
        },
    }


def read_json_file(path, fallback):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return fallback
    except (OSError, json.JSONDecodeError):
        return fallback


def write_json_file(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = f"{path}.{os.getpid()}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=True, indent=2)
        f.write("\n")
    os.replace(tmp_path, path)


def public_channel(channel):
    return {
        "id": channel.get("id", ""),
        "playlistId": channel.get("playlistId", ""),
        "name": channel.get("name", "Unnamed channel"),
        "group": channel.get("group", ""),
        "logo": channel.get("logo", ""),
        "url": channel.get("url", ""),
    }


def parse_extinf_attrs(text):
    attrs = {}
    for match in re.finditer(r'([A-Za-z0-9_-]+)="([^"]*)"', text):
        attrs[match.group(1).lower()] = match.group(2).strip()
    return attrs


def absolutize_url(base_url, item_url):
    parsed = urlsplit(item_url)
    if parsed.scheme:
        return item_url
    if item_url.startswith("//"):
        return f"{urlsplit(base_url).scheme}:{item_url}"
    if item_url.startswith("/"):
        base = urlsplit(base_url)
        return f"{base.scheme}://{base.netloc}{item_url}"
    prefix = base_url.rsplit("/", 1)[0] if "/" in urlsplit(base_url).path else base_url.rstrip("/")
    return f"{prefix}/{item_url}"


def parse_m3u(body, playlist_id, playlist_url):
    channels = []
    pending = None
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#EXTINF"):
            attrs = parse_extinf_attrs(line)
            name = line.rsplit(",", 1)[-1].strip() if "," in line else ""
            pending = {
                "name": attrs.get("tvg-name") or name or "Unnamed channel",
                "group": attrs.get("group-title", ""),
                "logo": attrs.get("tvg-logo", ""),
            }
            continue
        if line.startswith("#"):
            continue
        if pending is None:
            pending = {"name": line.rsplit("/", 1)[-1] or "Unnamed channel", "group": "", "logo": ""}
        channel_url = absolutize_url(playlist_url, line)
        channel_id = uuid.uuid5(uuid.NAMESPACE_URL, f"{playlist_id}:{channel_url}").hex
        channels.append(
            {
                "id": channel_id,
                "playlistId": playlist_id,
                "name": pending["name"],
                "group": pending["group"],
                "logo": pending["logo"],
                "url": channel_url,
            }
        )
        pending = None
    return channels


def playlist_summary(playlist):
    return {
        "id": playlist.get("id", ""),
        "name": playlist.get("name", ""),
        "url": playlist.get("url", ""),
        "channelCount": len(playlist.get("channels", [])),
        "lastFetchedAt": playlist.get("lastFetchedAt"),
        "lastError": playlist.get("lastError"),
    }


class NoCacheHandler(http.server.SimpleHTTPRequestHandler):
    channel_id = os.environ.get("CHANNEL_ID", "cctv5-4k-cn")
    channel_name = os.environ.get("CHANNEL_NAME", "CCTV5 4K Chinese")
    channel_number = os.environ.get("CHANNEL_NUMBER", "5")
    channel_group = os.environ.get("CHANNEL_GROUP", "Sports")
    public_base_url = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    state_dir = os.environ.get("STATE_DIR", "/state")
    offset_state = os.environ.get("OFFSET_STATE", "/state/last_sync_offset.json")
    hls_dir = os.environ.get("OUT_DIR", "/hls")
    web_dir = os.environ.get("WEB_DIR", "/app/web")
    manager_state_path = os.environ.get("MANAGER_STATE", "/state/stream_manager.json")
    screenshot_dir = os.environ.get("SCREENSHOT_DIR", "/state/screenshots")

    def public_url(self, path):
        if self.public_base_url:
            return f"{self.public_base_url}{path}"

        host = self.headers.get("Host", "")
        if not host:
            host = f"127.0.0.1:{self.server.server_address[1]}"
        return f"http://{host}{path}"

    def manager_state(self):
        state = read_json_file(self.manager_state_path, default_manager_state())
        default_state = default_manager_state()
        for key, value in default_state.items():
            state.setdefault(key, value)
        state["profile"].setdefault("videoPrimary", None)
        state["profile"].setdefault("videoFallbacks", [])
        state["profile"].setdefault("audio", None)
        state["profile"].setdefault("manualOffsetSeconds", None)
        state["profile"].setdefault("autoAdjustWhenNoTimer", False)
        state["resolution"].setdefault("failureCount", 0)
        return state

    def save_manager_state(self, state):
        write_json_file(self.manager_state_path, state)

    def read_request_json(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length > MAX_BODY_BYTES:
            self.respond_json({"error": "Request body is too large."}, status=413)
            return None
        body = self.rfile.read(length)
        try:
            return json.loads(body.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            self.respond_json({"error": "Request body must be valid JSON."}, status=400)
            return None

    def all_channels(self, state):
        channels = []
        for playlist in state.get("playlists", []):
            channels.extend(public_channel(channel) for channel in playlist.get("channels", []))
        return channels

    def channel_by_id(self, state, channel_id):
        if not channel_id:
            return None
        for playlist in state.get("playlists", []):
            for channel in playlist.get("channels", []):
                if channel.get("id") == channel_id:
                    return public_channel(channel)
        return None

    def resolved_profile(self, state):
        profile = state.get("profile", {})
        primary = self.channel_by_id(state, profile.get("videoPrimary"))
        fallbacks = [self.channel_by_id(state, item) for item in profile.get("videoFallbacks", [])]
        audio = self.channel_by_id(state, profile.get("audio"))
        fallbacks = [item for item in fallbacks if item]
        current_video = primary or (fallbacks[0] if fallbacks else None)
        current_audio = audio

        resolution = state.get("resolution", {})
        if current_video:
            resolution["currentVideoUrl"] = current_video["url"]
            resolution["activeSource"] = current_video["name"]
            resolution["lastOutcome"] = "Resolved selected primary video channel."
            resolution["lastResolvedAt"] = utc_stamp()
        if current_audio:
            resolution["currentAudioUrl"] = current_audio["url"]
        if not current_video and not current_audio:
            resolution["currentVideoUrl"] = os.environ.get("VIDEO_URL", "")
            resolution["currentAudioUrl"] = os.environ.get("AUDIO_URL", "")
            resolution["activeSource"] = "environment"
            resolution.setdefault(
                "lastOutcome",
                "Using VIDEO_URL and AUDIO_URL from environment until a profile is saved.",
            )

        return {
            "videoPrimary": primary,
            "videoFallbacks": fallbacks,
            "audio": audio,
            "manualOffsetSeconds": profile.get("manualOffsetSeconds"),
            "autoAdjustWhenNoTimer": bool(profile.get("autoAdjustWhenNoTimer", False)),
            "resolution": resolution,
        }

    def hls_status(self):
        index_path = os.path.join(self.hls_dir, "index.m3u8")
        exists = os.path.exists(index_path)
        segments = []
        newest_mtime = None
        if os.path.isdir(self.hls_dir):
            for entry in os.scandir(self.hls_dir):
                if not entry.name.endswith(".ts"):
                    continue
                stat = entry.stat()
                newest_mtime = stat.st_mtime if newest_mtime is None else max(newest_mtime, stat.st_mtime)
                segments.append({"name": entry.name, "size": stat.st_size, "modifiedAt": utc_stamp(stat.st_mtime)})
        index_mtime = os.path.getmtime(index_path) if exists else None
        return {
            "playlistUrl": self.public_url("/index.m3u8"),
            "isLive": bool(exists and index_mtime and time.time() - index_mtime < 30),
            "indexModifiedAt": utc_stamp(index_mtime) if index_mtime else None,
            "segmentCount": len(segments),
            "latestSegmentAt": utc_stamp(newest_mtime) if newest_mtime else None,
        }

    def offset_status(self):
        data = read_json_file(self.offset_state, {})
        offset = data.get("offset_seconds")
        return {
            "offsetSeconds": offset,
            "updatedAt": utc_stamp(data["updated_at_unix"]) if data.get("updated_at_unix") else None,
            "videoSampleCount": data.get("video_sample_count"),
            "audioSampleCount": data.get("audio_sample_count"),
            "matchCount": data.get("match_count"),
            "policy": "If OCR finds no on-screen timer, keep the previous offset and do not auto-adjust.",
        }

    def screenshot_items(self):
        if not os.path.isdir(self.screenshot_dir):
            return []
        items = []
        for entry in os.scandir(self.screenshot_dir):
            if not entry.is_file():
                continue
            if not entry.name.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
                continue
            stat = entry.stat()
            items.append(
                {
                    "name": entry.name,
                    "url": f"/api/screenshots/{entry.name}",
                    "size": stat.st_size,
                    "createdAt": utc_stamp(stat.st_mtime),
                }
            )
        return sorted(items, key=lambda item: item["createdAt"], reverse=True)[:SCREENSHOT_LIMIT]

    def log_tail(self, lines=160):
        paths = ["/tmp/live_synced_http.log", "/tmp/live_synced_status.log"]
        output = []
        for path in paths:
            if not os.path.exists(path):
                continue
            output.append(f"==> {path} <==")
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    output.extend(f.readlines()[-lines:])
            except OSError as exc:
                output.append(f"could not read log: {exc}\n")
        return "".join(output).strip()

    def do_GET(self):
        request_path = urlsplit(self.path).path
        if request_path == "/":
            self.serve_web_file("index.html")
            return

        if request_path.startswith("/assets/"):
            self.serve_web_file(request_path.removeprefix("/assets/"))
            return

        if request_path == "/api/state":
            state = self.manager_state()
            self.respond_json(
                {
                    "playlists": [playlist_summary(item) for item in state.get("playlists", [])],
                    "channels": self.all_channels(state),
                    "profile": self.resolved_profile(state),
                    "status": {
                        "hls": self.hls_status(),
                        "offset": self.offset_status(),
                        "screenshots": self.screenshot_items(),
                    },
                }
            )
            return

        if request_path == "/api/logs":
            self.respond_json({"logs": self.log_tail()})
            return

        if request_path.startswith("/api/screenshots/"):
            self.serve_screenshot(request_path.removeprefix("/api/screenshots/"))
            return

        if request_path == "/emby.m3u":
            self.respond_text(
                "audio/x-mpegurl",
                "\n".join(
                    [
                        "#EXTM3U",
                        (
                            '#EXTINF:-1 '
                            f'tvg-id="{self.channel_id}" '
                            f'tvg-name="{self.channel_name}" '
                            f'tvg-chno="{self.channel_number}" '
                            f'tvg-group="{self.channel_group}",'
                            f"{self.channel_name}"
                        ),
                        self.public_url("/index.m3u8"),
                        "",
                    ]
                ),
            )
            return

        if request_path == "/cctv5.strm":
            self.respond_text("text/plain", f'{self.public_url("/index.m3u8")}\n')
            return

        if request_path == "/guide.xml":
            now = int(time.time())
            start = time.strftime("%Y%m%d%H%M%S +0000", time.gmtime(now - 3600))
            stop = time.strftime("%Y%m%d%H%M%S +0000", time.gmtime(now + 86400))
            self.respond_text(
                "application/xml",
                "\n".join(
                    [
                        '<?xml version="1.0" encoding="UTF-8"?>',
                        '<tv generator-info-name="live-sync-cctv">',
                        f'  <channel id="{self.channel_id}">',
                        f"    <display-name>{self.channel_name}</display-name>",
                        "  </channel>",
                        f'  <programme start="{start}" stop="{stop}" channel="{self.channel_id}">',
                        "    <title>Live</title>",
                        "    <category>Sports</category>",
                        "  </programme>",
                        "</tv>",
                        "",
                    ]
                ),
            )
            return

        super().do_GET()

    def do_POST(self):
        request_path = urlsplit(self.path).path
        if request_path == "/api/playlists":
            payload = self.read_request_json()
            if payload is None:
                return
            playlist_url = str(payload.get("url", "")).strip()
            name = str(payload.get("name", "")).strip()
            if not playlist_url.startswith(("http://", "https://")):
                self.respond_json({"error": "Playlist URL must start with http:// or https://."}, status=400)
                return

            playlist_id = uuid.uuid5(uuid.NAMESPACE_URL, playlist_url).hex
            state = self.manager_state()
            playlist = next((item for item in state["playlists"] if item.get("id") == playlist_id), None)
            if playlist is None:
                playlist = {"id": playlist_id, "name": name or playlist_url, "url": playlist_url, "channels": []}
                state["playlists"].append(playlist)
            else:
                playlist["name"] = name or playlist.get("name") or playlist_url
                playlist["url"] = playlist_url
            self.fetch_playlist(playlist)
            self.save_manager_state(state)
            self.respond_json({"playlist": playlist_summary(playlist), "channels": self.all_channels(state)})
            return

        if request_path == "/api/profile":
            payload = self.read_request_json()
            if payload is None:
                return
            state = self.manager_state()
            known_ids = {channel["id"] for channel in self.all_channels(state)}
            video_primary = payload.get("videoPrimary") or None
            video_fallbacks = [item for item in payload.get("videoFallbacks", []) if item in known_ids]
            audio = payload.get("audio") or None
            if video_primary and video_primary not in known_ids:
                self.respond_json({"error": "Selected primary video channel no longer exists."}, status=400)
                return
            if audio and audio not in known_ids:
                self.respond_json({"error": "Selected audio channel no longer exists."}, status=400)
                return
            manual_offset = payload.get("manualOffsetSeconds")
            if manual_offset in ("", None):
                manual_offset = None
            else:
                try:
                    manual_offset = round(float(manual_offset), 3)
                except (TypeError, ValueError):
                    self.respond_json({"error": "Manual offset must be a number of seconds."}, status=400)
                    return

            state["profile"] = {
                "videoPrimary": video_primary,
                "videoFallbacks": video_fallbacks,
                "audio": audio,
                "manualOffsetSeconds": manual_offset,
                "autoAdjustWhenNoTimer": bool(payload.get("autoAdjustWhenNoTimer", False)),
            }
            resolved = self.resolved_profile(state)
            state["resolution"] = resolved["resolution"]
            self.save_manager_state(state)
            self.respond_json({"profile": resolved})
            return

        if request_path == "/api/screenshots":
            self.capture_screenshot()
            return

        self.respond_json({"error": "Not found."}, status=404)

    def do_DELETE(self):
        request_path = urlsplit(self.path).path
        if request_path.startswith("/api/playlists/"):
            playlist_id = request_path.removeprefix("/api/playlists/").strip("/")
            state = self.manager_state()
            state["playlists"] = [item for item in state.get("playlists", []) if item.get("id") != playlist_id]
            known_ids = {channel["id"] for channel in self.all_channels(state)}
            state["profile"]["videoFallbacks"] = [
                item for item in state["profile"].get("videoFallbacks", []) if item in known_ids
            ]
            for key in ("videoPrimary", "audio"):
                if state["profile"].get(key) not in known_ids:
                    state["profile"][key] = None
            self.save_manager_state(state)
            self.respond_json({"playlists": [playlist_summary(item) for item in state.get("playlists", [])]})
            return
        self.respond_json({"error": "Not found."}, status=404)

    def fetch_playlist(self, playlist):
        request = Request(playlist["url"], headers={"User-Agent": "live-sync-stream-manager/1.0"})
        try:
            with urlopen(request, timeout=15) as response:
                body = response.read(MAX_PLAYLIST_BYTES + 1)
            if len(body) > MAX_PLAYLIST_BYTES:
                raise ValueError("playlist is larger than 8 MB")
            text = body.decode("utf-8-sig", errors="replace")
            channels = parse_m3u(text, playlist["id"], playlist["url"])
            if not channels:
                raise ValueError("playlist did not contain any playable channel URLs")
            playlist["channels"] = channels
            playlist["lastFetchedAt"] = utc_stamp()
            playlist["lastError"] = None
        except Exception as exc:
            playlist["lastError"] = str(exc)
            playlist.setdefault("channels", [])

    def capture_screenshot(self):
        os.makedirs(self.screenshot_dir, exist_ok=True)
        source = os.path.join(self.hls_dir, "index.m3u8")
        if not os.path.exists(source):
            self.respond_json({"error": "No HLS playlist exists yet, so a screenshot cannot be captured."}, status=409)
            return
        output = os.path.join(self.screenshot_dir, f"screen_{time.strftime('%Y%m%d_%H%M%S')}.jpg")
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            source,
            "-frames:v",
            "1",
            "-q:v",
            "3",
            output,
        ]
        proc = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        if proc.returncode != 0:
            self.respond_json({"error": proc.stderr.strip() or "ffmpeg could not capture a screenshot."}, status=500)
            return
        self.respond_json({"screenshots": self.screenshot_items()})

    def serve_web_file(self, relative_path):
        safe_path = os.path.normpath(relative_path).lstrip("/")
        path = os.path.join(self.web_dir, safe_path)
        root = os.path.abspath(self.web_dir)
        full = os.path.abspath(path)
        if not full.startswith(root) or not os.path.isfile(full):
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", self.guess_type(full))
        self.send_header("Content-Length", str(os.path.getsize(full)))
        self.end_headers()
        with open(full, "rb") as f:
            shutil.copyfileobj(f, self.wfile)

    def serve_screenshot(self, name):
        safe_name = Path(name).name
        path = os.path.join(self.screenshot_dir, safe_name)
        if not os.path.isfile(path):
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Content-Length", str(os.path.getsize(path)))
        self.end_headers()
        with open(path, "rb") as f:
            shutil.copyfileobj(f, self.wfile)

    def respond_text(self, content_type, body):
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def respond_json(self, payload, status=200):
        data = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()

    def guess_type(self, path):
        if path.endswith(".m3u8"):
            return "application/vnd.apple.mpegurl"
        if path.endswith(".m3u"):
            return "audio/x-mpegurl"
        if path.endswith(".ts"):
            return "video/mp2t"
        return super().guess_type(path)


class ReusableTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True


def main():
    if len(sys.argv) != 3:
        raise SystemExit("usage: serve_hls.py PORT DIRECTORY")

    port = int(sys.argv[1])
    directory = sys.argv[2]
    mimetypes.add_type("application/vnd.apple.mpegurl", ".m3u8")
    mimetypes.add_type("audio/x-mpegurl", ".m3u")
    mimetypes.add_type("video/mp2t", ".ts")

    handler = functools.partial(NoCacheHandler, directory=directory)
    with ReusableTCPServer(("0.0.0.0", port), handler) as httpd:
        httpd.serve_forever()


if __name__ == "__main__":
    main()
