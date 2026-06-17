#!/usr/bin/env python3
import argparse
import json
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass

import cv2


TIME_RE = re.compile(r"([0-9]{1,3})[:：.]([0-9]{2})")


@dataclass(frozen=True)
class Sample:
    media_time: float
    game_time: int
    text: str


@dataclass(frozen=True)
class Candidate:
    diff: float
    offset: float
    video: Sample
    audio: Sample


def run(cmd, check=True, capture=False):
    kwargs = {"text": True}
    if capture:
        kwargs.update({"stdout": subprocess.PIPE, "stderr": subprocess.PIPE})
    proc = subprocess.run(cmd, **kwargs)
    if check and proc.returncode != 0:
        raise RuntimeError(f"command failed: {' '.join(cmd)}")
    return proc


def duration(path):
    proc = run(
        [
            "ffprobe",
            "-hide_banner",
            "-loglevel",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            path,
        ],
        capture=True,
    )
    data = json.loads(proc.stdout)
    return float(data["format"]["duration"])


def extract_frame(video, at_seconds, out_path):
    run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{at_seconds:.3f}",
            "-i",
            video,
            "-frames:v",
            "1",
            "-update",
            "1",
            out_path,
        ]
    )


def ocr_time(frame_path, roi, scale=6):
    img = cv2.imread(frame_path)
    if img is None:
        return None

    h, w = img.shape[:2]
    x, y, rw, rh = roi
    crop = img[int(y * h) : int((y + rh) * h), int(x * w) : int((x + rw) * w)]
    if crop.size == 0:
        return None

    crop = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    blur = cv2.GaussianBlur(gray, (3, 3), 0)
    _, thr = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    thr = cv2.copyMakeBorder(thr, 30, 30, 30, 30, cv2.BORDER_CONSTANT, value=255)

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        ocr_input = f.name
    try:
        cv2.imwrite(ocr_input, thr)
        proc = run(
            [
                "tesseract",
                ocr_input,
                "stdout",
                "--psm",
                "7",
                "-c",
                "tessedit_char_whitelist=0123456789:",
            ],
            capture=True,
            check=False,
        )
    finally:
        try:
            os.unlink(ocr_input)
        except OSError:
            pass

    text = proc.stdout.strip().replace(" ", "")
    match = TIME_RE.search(text)
    if not match:
        return None
    minutes = int(match.group(1))
    seconds = int(match.group(2))
    if seconds >= 60:
        return None
    return minutes * 60 + seconds, text


def collect_samples(video, roi, start, end, step, workdir, label):
    samples = []
    t = start
    while t <= end + 1e-6:
        frame = os.path.join(workdir, f"{label}_{t:.3f}.jpg")
        extract_frame(video, t, frame)
        parsed = ocr_time(frame, roi)
        if parsed:
            game_time, text = parsed
            samples.append(Sample(t, game_time, text))
        t += step
    return samples


def clock_span(samples):
    if not samples:
        return 0
    times = [s.game_time for s in samples]
    return max(times) - min(times)


def estimate_offset(
    video_samples,
    audio_samples,
    *,
    min_samples=3,
    min_clock_span=5,
    max_offset=180,
    cluster_window=2.5,
    min_cluster=3,
):
    if len(video_samples) < min_samples:
        raise RuntimeError(f"not enough 4K clock samples: {len(video_samples)} < {min_samples}")
    if len(audio_samples) < min_samples:
        raise RuntimeError(f"not enough Chinese clock samples: {len(audio_samples)} < {min_samples}")

    video_span = clock_span(video_samples)
    audio_span = clock_span(audio_samples)
    if video_span < min_clock_span:
        raise RuntimeError(f"4K clock did not move enough: {video_span}s < {min_clock_span}s")
    if audio_span < min_clock_span:
        raise RuntimeError(f"Chinese clock did not move enough: {audio_span}s < {min_clock_span}s")

    candidates = []
    for vs in video_samples:
        for aus in audio_samples:
            # Target after trimming: the Chinese source's game clock should land
            # on the 4K source's game clock at the same output media time.
            offset = aus.media_time - vs.media_time - (aus.game_time - vs.game_time)
            if abs(offset) <= max_offset:
                candidates.append(Candidate(abs(offset), offset, vs, aus))
    if not candidates:
        raise RuntimeError(f"no feasible clock offset found within +/-{max_offset}s")

    candidates = sorted(candidates, key=lambda c: c.offset)
    best_cluster = []
    for i, candidate in enumerate(candidates):
        cluster = [c for c in candidates[i:] if c.offset - candidate.offset <= cluster_window]
        if len(cluster) > len(best_cluster):
            best_cluster = cluster

    video_hits = {c.video.media_time for c in best_cluster}
    audio_hits = {c.audio.media_time for c in best_cluster}
    if len(best_cluster) < min_cluster or len(video_hits) < 2 or len(audio_hits) < 2:
        raise RuntimeError(
            "clock offset was not reliable enough: "
            f"cluster={len(best_cluster)}, video_hits={len(video_hits)}, audio_hits={len(audio_hits)}"
        )

    offsets = sorted(c.offset for c in best_cluster)
    median = offsets[len(offsets) // 2]
    best = min(best_cluster, key=lambda c: abs(c.offset - median))
    return median, best, best_cluster


def load_last_offset(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        print(f"could not read last offset state {path}: {exc}")
        return None

    try:
        return float(data["offset_seconds"])
    except (KeyError, TypeError, ValueError) as exc:
        print(f"last offset state {path} is invalid: {exc}")
        return None


def save_last_offset(path, offset, *, video_samples, audio_samples, matches):
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)

    data = {
        "offset_seconds": round(float(offset), 3),
        "updated_at_unix": int(time.time()),
        "video_sample_count": len(video_samples),
        "audio_sample_count": len(audio_samples),
        "match_count": len(matches),
    }
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=True, indent=2)
        f.write("\n")
    os.replace(tmp_path, path)


def merge(video_source, audio_source, output, duration_seconds, offset_seconds):
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-y",
        "-i",
        video_source,
    ]
    if offset_seconds > 0.001:
        cmd += ["-ss", f"{offset_seconds:.3f}", "-i", audio_source]
    elif offset_seconds < -0.001:
        cmd += ["-itsoffset", f"{-offset_seconds:.3f}", "-i", audio_source]
    else:
        cmd += ["-i", audio_source]
    cmd += [
        "-t",
        str(duration_seconds),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-map",
        "0:a:0?",
        "-c",
        "copy",
        "-metadata:s:a:0",
        "language=zho",
        "-metadata:s:a:0",
        "title=Chinese commentary",
        "-metadata:s:a:1",
        "language=und",
        "-metadata:s:a:1",
        "title=Original audio",
        "-disposition:a:0",
        "default",
        output,
    ]
    run(cmd)


def main():
    parser = argparse.ArgumentParser(description="Align two football feeds by OCRing the scoreboard clock.")
    parser.add_argument("--video", default="/state/original_4k_60s.mkv")
    parser.add_argument("--audio-video", default="/state/original_chinese_60s.mkv")
    parser.add_argument("--output", default="/state/merged_auto_aligned.mkv")
    parser.add_argument("--duration", type=float, default=30)
    parser.add_argument("--start", type=float, default=5)
    parser.add_argument("--step", type=float, default=5)
    parser.add_argument("--scan", type=float, default=50)
    parser.add_argument("--video-roi", default="0.050,0.050,0.070,0.050")
    parser.add_argument("--audio-roi", default="0.885,0.085,0.075,0.060")
    parser.add_argument("--fallback", choices=["last", "direct", "fail"], default="last")
    parser.add_argument("--fallback-offset", type=float, default=0)
    parser.add_argument("--offset-state", default="/state/last_sync_offset.json")
    parser.add_argument("--min-samples", type=int, default=3)
    parser.add_argument("--min-clock-span", type=float, default=5)
    parser.add_argument("--max-offset", type=float, default=180)
    parser.add_argument("--cluster-window", type=float, default=2.5)
    parser.add_argument("--min-cluster", type=int, default=3)
    parser.add_argument("--no-merge", action="store_true")
    args = parser.parse_args()

    video_roi = tuple(float(x) for x in args.video_roi.split(","))
    audio_roi = tuple(float(x) for x in args.audio_roi.split(","))
    scan_end = min(args.scan, duration(args.video) - 1, duration(args.audio_video) - 1)

    with tempfile.TemporaryDirectory(prefix="sync_ocr_") as workdir:
        video_samples = collect_samples(args.video, video_roi, args.start, scan_end, args.step, workdir, "video")
        audio_samples = collect_samples(args.audio_video, audio_roi, args.start, scan_end, args.step, workdir, "audio")

    print(f"video samples: {[(s.media_time, s.text) for s in video_samples]}")
    print(f"audio samples: {[(s.media_time, s.text) for s in audio_samples]}")

    alignment_mode = "aligned"
    try:
        offset, best, candidates = estimate_offset(
            video_samples,
            audio_samples,
            min_samples=args.min_samples,
            min_clock_span=args.min_clock_span,
            max_offset=args.max_offset,
            cluster_window=args.cluster_window,
            min_cluster=args.min_cluster,
        )
        print(
            "best match: "
            f"video t={best.video.media_time:.3f}s clock={best.video.text}, "
            f"audio t={best.audio.media_time:.3f}s clock={best.audio.text}"
        )
        print(f"estimated audio offset: {offset:.3f}s from {len(candidates)} clustered matches")
        save_last_offset(
            args.offset_state,
            offset,
            video_samples=video_samples,
            audio_samples=audio_samples,
            matches=candidates,
        )
        print(f"saved last reliable offset to {args.offset_state}")
    except RuntimeError as exc:
        if args.fallback == "fail":
            raise
        print(f"no reliable clock alignment: {exc}")
        if args.fallback == "last":
            last_offset = load_last_offset(args.offset_state)
            if last_offset is not None:
                alignment_mode = "last-offset"
                offset = last_offset
                print(f"using last reliable offset from {args.offset_state}: {offset:.3f}s")
            else:
                alignment_mode = "direct"
                offset = args.fallback_offset
                print(f"no saved offset found; using direct fallback offset: {offset:.3f}s")
        else:
            alignment_mode = "direct"
            offset = args.fallback_offset
            print(f"using direct merge fallback offset: {offset:.3f}s")

    if not args.no_merge:
        merge(args.video, args.audio_video, args.output, args.duration, offset)
        print(f"wrote {args.output} ({alignment_mode})")


if __name__ == "__main__":
    main()
