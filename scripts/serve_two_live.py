#!/usr/bin/env python3
"""Publish two local video files as rolling HLS "live" streams on port 8800.

Use case: maintain a local dual-feed upstream that other tooling can consume.
This script turns two local recordings into live-like HLS feeds (real-time
pacing, rolling window, EXT-X-PROGRAM-DATE-TIME), serves both over one HTTP
port, and prints the two master URLs.

Why this is "live" and not VOD:
  -re                 read input at native frame rate (real-time, not instant)
  -stream_loop -1     loop forever so the feed never ends
  hls_list_size N     rolling window: old segments drop off the playlist
  delete_segments     old .ts files are removed from disk
  program_date_time   each segment stamped with wall-clock time (PDT)

Controlling the feed pair:
  Both feeds share ONE wall-clock origin, so the offset between them is fully
  under your control:
    --skew S     start feed B `S` seconds into its file relative to feed A
                 (positive => B is ahead in match-content / lags in wall-time).
                 This is the known wall-clock skew between the two feeds.
  If the two files already differ in commentary timing, --skew stacks on top.

Example:
  python3 scripts/serve_two_live.py feedA.mp4 feedB.mp4 --skew 8 --port 8800
    -> http://<host>:8800/A/master.m3u8
       http://<host>:8800/B/master.m3u8

Requires: ffmpeg. Pure stdlib otherwise.
"""
from __future__ import annotations

import argparse
import http.server
import os
import shutil
import signal
import socket
import socketserver
import subprocess
import threading
import time
from dataclasses import dataclass, field
from functools import partial
from urllib.parse import urlsplit


def build_ffmpeg_cmd(infile, outdir, *, skew, seg_time, list_size,
                     reencode, vcodec, preset, abitrate):
    """ffmpeg command that loops `infile` into a rolling HLS live in `outdir`."""
    pre_input = []
    if skew and skew > 0:
        pre_input += ["-ss", f"{skew:.3f}"]

    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning",
        "-re", "-stream_loop", "-1",
        *pre_input,
        "-i", infile,
    ]

    if reencode:
        cmd += [
            "-c:v", vcodec, "-preset", preset, "-g", "48", "-sc_threshold", "0",
            "-c:a", "aac", "-b:a", abitrate, "-ac", "2",
        ]
    else:
        cmd += ["-c", "copy"]

    cmd += [
        "-f", "hls",
        "-hls_time", str(seg_time),
        "-hls_list_size", str(list_size),
        "-hls_flags", "delete_segments+program_date_time+independent_segments",
        "-hls_segment_type", "mpegts",
        "-hls_segment_filename", os.path.join(outdir, "seg_%d.ts"),
        os.path.join(outdir, "master.m3u8"),
    ]
    return cmd


def wait_for_playlist(playlist_path, proc, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(playlist_path) and os.path.getsize(playlist_path) > 0:
            return
        if proc.poll() is not None:
            raise RuntimeError(f"ffmpeg exited with code {proc.returncode}")
        time.sleep(0.5)
    raise RuntimeError(f"playlist not ready: {playlist_path}")


@dataclass
class Generation:
    feed_name: str
    name: str
    outdir: str
    playlist_path: str
    proc: subprocess.Popen
    log_handle: object
    stop_after: float = 0.0


@dataclass
class FeedConfig:
    name: str
    infile: str
    skew: float


@dataclass
class FeedState:
    config: FeedConfig
    root: str
    common: dict
    generation_counter: int = 0
    active: Generation | None = None
    retired: list[Generation] = field(default_factory=list)

    @property
    def feed_dir(self):
        return os.path.join(self.root, self.config.name)

    @property
    def log_path(self):
        return os.path.join(self.root, f"{self.config.name}.log")

    @property
    def marker_path(self):
        return os.path.join(self.feed_dir, "current.txt")

    def next_generation_name(self):
        self.generation_counter += 1
        return f"g{self.generation_counter:06d}"

    def start_generation(self):
        os.makedirs(self.feed_dir, exist_ok=True)
        gen_name = self.next_generation_name()
        outdir = os.path.join(self.feed_dir, gen_name)
        os.makedirs(outdir, exist_ok=True)
        cmd = build_ffmpeg_cmd(self.config.infile, outdir, skew=self.config.skew, **self.common)
        log_handle = open(self.log_path, "a", buffering=1, encoding="utf-8")
        print(f"[feed {self.config.name}] {self.config.infile}  skew={self.config.skew}s  -> "
              f"{self.config.name}/{gen_name}/master.m3u8", flush=True)
        proc = subprocess.Popen(cmd, stdout=log_handle, stderr=subprocess.STDOUT)
        playlist_path = os.path.join(outdir, "master.m3u8")
        return Generation(self.config.name, gen_name, outdir, playlist_path, proc, log_handle)

    def stop_generation(self, generation):
        if generation.proc.poll() is None:
            generation.proc.terminate()
        try:
            generation.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            generation.proc.kill()
            generation.proc.wait(timeout=5)
        if getattr(generation.log_handle, "closed", False) is False:
            generation.log_handle.close()

    def publish_generation(self, generation):
        os.makedirs(self.feed_dir, exist_ok=True)
        with open(self.marker_path, "w", encoding="utf-8") as fh:
            fh.write(generation.name + "\n")

    def render_playlist(self):
        generation = self.active
        if generation is None:
            return ""
        if not os.path.exists(generation.playlist_path):
            return ""
        with open(generation.playlist_path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
        rendered = []
        for line in lines:
            if line and not line.startswith("#"):
                rendered.append(f"{generation.name}/{line}")
            else:
                rendered.append(line)
        return "\n".join(rendered) + ("\n" if rendered else "")


class FeedManager:
    def __init__(self, root, feed_configs, common, reload_grace):
        self.root = root
        self.reload_grace = reload_grace
        self.lock = threading.RLock()
        self.states = {
            cfg.name: FeedState(cfg, root, dict(common))
            for cfg in feed_configs
        }

    def reset_root(self):
        shutil.rmtree(self.root, ignore_errors=True)
        os.makedirs(self.root, exist_ok=True)
        for state in self.states.values():
            os.makedirs(state.feed_dir, exist_ok=True)

    def _prepare_generations(self):
        pending = {}
        try:
            for name, state in self.states.items():
                generation = state.start_generation()
                pending[name] = generation
            for generation in pending.values():
                wait_for_playlist(generation.playlist_path, generation.proc, timeout=30)
            return pending
        except Exception:
            for name, generation in pending.items():
                state = self.states[name]
                try:
                    state.stop_generation(generation)
                except Exception:
                    pass
                shutil.rmtree(generation.outdir, ignore_errors=True)
            raise

    def start_initial(self):
        self.reset_root()
        pending = self._prepare_generations()
        with self.lock:
            for name, generation in pending.items():
                state = self.states[name]
                state.active = generation
                state.retired = []
                state.publish_generation(generation)

    def reload(self):
        pending = self._prepare_generations()
        now_ts = time.time()
        with self.lock:
            for name, generation in pending.items():
                state = self.states[name]
                previous = state.active
                state.active = generation
                state.publish_generation(generation)
                if previous is not None:
                    previous.stop_after = now_ts + self.reload_grace
                    state.retired.append(previous)

    def render_playlist(self, feed_name):
        with self.lock:
            state = self.states.get(feed_name)
            if not state:
                return ""
            return state.render_playlist()

    def cleanup(self):
        exited = []
        now_ts = time.time()
        with self.lock:
            for state in self.states.values():
                active = state.active
                if active is not None and active.proc.poll() is not None:
                    exited.append((state.config.name, active.proc.returncode))
                kept = []
                for generation in state.retired:
                    if generation.stop_after and now_ts >= generation.stop_after and generation.proc.poll() is None:
                        state.stop_generation(generation)
                    if generation.stop_after and now_ts >= generation.stop_after and generation.proc.poll() is not None:
                        shutil.rmtree(generation.outdir, ignore_errors=True)
                        if getattr(generation.log_handle, "closed", False) is False:
                            generation.log_handle.close()
                        continue
                    kept.append(generation)
                state.retired = kept
        return exited

    def stop_all(self):
        with self.lock:
            for state in self.states.values():
                if state.active is not None:
                    try:
                        state.stop_generation(state.active)
                    except Exception:
                        pass
                for generation in state.retired:
                    try:
                        state.stop_generation(generation)
                    except Exception:
                        pass


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, directory=None, manager=None, **kwargs):
        self.manager = manager
        super().__init__(*args, directory=directory, **kwargs)

    def log_message(self, *a):
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()

    def _send_playlist(self, feed_name):
        body = self.manager.render_playlist(feed_name)
        if not body:
            payload = b"HLS playlist is not ready\n"
            self.send_response(503)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Retry-After", "2")
            self.end_headers()
            self.wfile.write(payload)
            return
        payload = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.apple.mpegurl")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/A/master.m3u8":
            return self._send_playlist("A")
        if path == "/B/master.m3u8":
            return self._send_playlist("B")
        return super().do_GET()


def serve_http(root, port, manager):
    handler = partial(QuietHandler, directory=root, manager=manager)
    httpd = ReusableTCPServer(("0.0.0.0", port), handler)
    httpd.daemon_threads = True
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file_a", help="local video for feed A")
    ap.add_argument("file_b", help="local video for feed B")
    ap.add_argument("--port", type=int, default=8800)
    ap.add_argument("--root", default="work/two_live", help="HLS output dir")
    ap.add_argument("--skew", type=float, default=0.0,
                    help="start feed B this many seconds into its file (ground-truth delay)")
    ap.add_argument("--skew-a", type=float, default=0.0,
                    help="optional skew applied to feed A as well")
    ap.add_argument("--seg-time", type=float, default=4.0)
    ap.add_argument("--list-size", type=int, default=10)
    ap.add_argument("--copy", action="store_true",
                    help="stream-copy instead of re-encoding (needs compatible input)")
    ap.add_argument("--vcodec", default="libx264")
    ap.add_argument("--preset", default="veryfast")
    ap.add_argument("--abitrate", default="128k")
    ap.add_argument("--reload-grace", type=float, default=90.0,
                    help="keep old generation segments this many seconds after a reload")
    ap.add_argument("--host", default=None,
                    help="hostname to print in URLs (default: autodetect)")
    args = ap.parse_args()

    for f in (args.file_a, args.file_b):
        if not os.path.isfile(f):
            ap.error(f"no such file: {f}")
    if not shutil.which("ffmpeg"):
        ap.error("ffmpeg not found on PATH")

    common = dict(
        seg_time=args.seg_time,
        list_size=args.list_size,
        reencode=not args.copy,
        vcodec=args.vcodec,
        preset=args.preset,
        abitrate=args.abitrate,
    )
    manager = FeedManager(
        args.root,
        [
            FeedConfig("A", args.file_a, args.skew_a),
            FeedConfig("B", args.file_b, args.skew),
        ],
        common,
        reload_grace=max(args.reload_grace, args.seg_time * args.list_size),
    )
    manager.start_initial()
    httpd = serve_http(args.root, args.port, manager)

    host = args.host or _guess_host()
    print("\n[serve] both feeds live.\n", flush=True)
    print("=" * 56)
    print(f"  Feed A : http://{host}:{args.port}/A/master.m3u8")
    print(f"  Feed B : http://{host}:{args.port}/B/master.m3u8")
    print(f"  ground-truth B-minus-A content skew : {args.skew - args.skew_a:+.3f}s")
    print("=" * 56)
    print("Ctrl-C to stop.\n", flush=True)

    stop_event = threading.Event()
    reload_event = threading.Event()

    def shutdown(*_):
        stop_event.set()

    def reload_feeds(*_):
        reload_event.set()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGHUP, reload_feeds)

    try:
        while not stop_event.is_set():
            if reload_event.is_set():
                reload_event.clear()
                print("\n[serve] reloading upstream ffmpeg feeds...", flush=True)
                try:
                    manager.reload()
                    print("[serve] refreshed feeds live.\n", flush=True)
                except Exception as exc:
                    print(f"[serve] reload failed: {exc}\n", flush=True)
            exited = manager.cleanup()
            if exited:
                for name, code in exited:
                    print(f"[feed {name}] ffmpeg exited (code {code}); see work/two_live/{name}.log", flush=True)
                stop_event.set()
                continue
            stop_event.wait(1.0)
    finally:
        print("\n[serve] stopping...", flush=True)
        manager.stop_all()
        httpd.shutdown()
        print("[serve] done.", flush=True)


def _guess_host():
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        sock.close()
        return ip
    except OSError:
        return "127.0.0.1"


class ReusableTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True


if __name__ == "__main__":
    main()
