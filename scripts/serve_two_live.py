#!/usr/bin/env python3
"""Publish two local video files as rolling HLS "live" streams for testing.

Use case: you have two recordings of the SAME match with DIFFERENT commentary.
This script turns each into a live-like HLS feed (real-time pacing, rolling
window, EXT-X-PROGRAM-DATE-TIME), serves both over one HTTP port, and prints
the two master URLs to feed into the alignment pipeline.

Why this is "live" and not VOD:
  -re                 read input at native frame rate (real-time, not instant)
  -stream_loop -1     loop forever so the feed never ends
  hls_list_size N     rolling window: old segments drop off the playlist
  delete_segments     old .ts files are removed from disk
  program_date_time   each segment stamped with wall-clock time (PDT)

Controlling the experiment:
  Both feeds share ONE wall-clock origin, so the offset between them is fully
  under your control:
    --skew S     start feed B `S` seconds into its file relative to feed A
                 (positive => B is ahead in match-content / lags in wall-time).
                 This is your ground-truth delay to recover.
  If the two files already differ in commentary timing, --skew stacks on top.

Example:
  python3 scripts/serve_two_live.py feedA.mp4 feedB.mp4 --skew 8 --port 8800
    -> http://<host>:8800/A/master.m3u8
       http://<host>:8800/B/master.m3u8

Requires: ffmpeg. Pure stdlib otherwise.
"""
import argparse
import http.server
import os
import shutil
import signal
import socketserver
import subprocess
import sys
import threading
from functools import partial


def build_ffmpeg_cmd(infile, outdir, *, skew, seg_time, list_size,
                     reencode, vcodec, preset, abitrate):
    """ffmpeg command that loops `infile` into a rolling HLS live in `outdir`."""
    pre_input = []
    # Seeking before -i with -ss starts the content `skew` seconds in. We do an
    # input seek (fast) on a looped source. For a one-shot skew on a looped file
    # the seek applies to the first pass; combine with a long file for clean
    # behavior. For most test clips skew << duration so this is fine.
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
        # copy is faster but requires keyframe-aligned segments; risky for
        # arbitrary inputs, so default path re-encodes. Kept for completeness.
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


def start_feed(name, infile, root, **kw):
    outdir = os.path.join(root, name)
    if os.path.exists(outdir):
        shutil.rmtree(outdir)
    os.makedirs(outdir, exist_ok=True)
    cmd = build_ffmpeg_cmd(infile, outdir, **kw)
    log = open(os.path.join(root, f"{name}.log"), "w")
    print(f"[feed {name}] {infile}  skew={kw['skew']}s  -> {name}/master.m3u8")
    proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)
    return proc, log


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def end_headers(self):
        # HLS players need fresh playlists + permissive CORS for browser tests.
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()


def serve_http(root, port):
    handler = partial(QuietHandler, directory=root)
    httpd = socketserver.ThreadingTCPServer(("0.0.0.0", port), handler)
    httpd.daemon_threads = True
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd


def wait_for_playlists(root, names, timeout=30):
    import time as _t
    deadline = _t.time() + timeout
    pending = {n: os.path.join(root, n, "master.m3u8") for n in names}
    while pending and _t.time() < deadline:
        for n, path in list(pending.items()):
            if os.path.exists(path) and os.path.getsize(path) > 0:
                pending.pop(n)
        if pending:
            _t.sleep(0.5)
    return not pending


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
    ap.add_argument("--host", default=None,
                    help="hostname to print in URLs (default: autodetect)")
    args = ap.parse_args()

    for f in (args.file_a, args.file_b):
        if not os.path.isfile(f):
            ap.error(f"no such file: {f}")
    if not shutil.which("ffmpeg"):
        ap.error("ffmpeg not found on PATH")

    os.makedirs(args.root, exist_ok=True)
    common = dict(
        seg_time=args.seg_time, list_size=args.list_size,
        reencode=not args.copy, vcodec=args.vcodec,
        preset=args.preset, abitrate=args.abitrate,
    )

    procs = []
    procs.append(("A", *start_feed("A", args.file_a, args.root, skew=args.skew_a, **common)))
    procs.append(("B", *start_feed("B", args.file_b, args.root, skew=args.skew, **common)))

    httpd = serve_http(args.root, args.port)

    host = args.host or _guess_host()
    print("\n[serve] waiting for first segments...")
    if wait_for_playlists(args.root, ["A", "B"]):
        print("[serve] both feeds live.\n")
    else:
        print("[serve] WARNING: playlists not ready yet; check work/two_live/*.log\n")

    print("=" * 56)
    print(f"  Feed A : http://{host}:{args.port}/A/master.m3u8")
    print(f"  Feed B : http://{host}:{args.port}/B/master.m3u8")
    print(f"  ground-truth B-minus-A content skew : {args.skew - args.skew_a:+.3f}s")
    print("=" * 56)
    print("Ctrl-C to stop.\n")

    stop = threading.Event()

    def shutdown(*_):
        stop.set()
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    try:
        while not stop.is_set():
            for name, proc, _log in procs:
                if proc.poll() is not None:
                    print(f"[feed {name}] ffmpeg exited (code {proc.returncode}); "
                          f"see work/two_live/{name}.log")
                    stop.set()
            stop.wait(1.0)
    finally:
        print("\n[serve] stopping...")
        for _name, proc, log in procs:
            if proc.poll() is None:
                proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            log.close()
        httpd.shutdown()
        print("[serve] done.")


def _guess_host():
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


if __name__ == "__main__":
    main()
