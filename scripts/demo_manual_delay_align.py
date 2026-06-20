#!/usr/bin/env python3
"""Demo: manual-delay alignment proof-of-concept.

We can't easily get two *different* live commentary feeds of the same match on
demand, so this demo manufactures a controlled experiment from a single channel:

  1. Record N seconds from the source HLS channel  -> base.ts
  2. Build "stream A" = base                        (the video source)
     Build "stream B" = base shifted by a KNOWN delay (the audio source)
        - B starts `--inject-delay` seconds later than A, i.e. at wall-clock
          time T, B is showing what A showed at T-delay.
  3. Estimate the A->B offset purely from audio (energy-envelope cross
     correlation) -- the same technique that works on real feeds because the
     shared crowd/ambient sound correlates even when commentary differs.
  4. Merge A-video + B-audio, shifting B back by the estimated offset, and
     report recovered-vs-injected error.

Because A and B share identical audio here, the estimator should recover the
injected delay almost exactly. That validates the measurement + merge path;
real feeds just add commentary noise on top, which the envelope step suppresses.

Requires: ffmpeg/ffprobe, numpy. (No scipy/av needed.)
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import wave

import numpy as np

SRC_DEFAULT = "https://iptv.852851.xyz/ch/6e6fbc3c7498ef8dd68ef39fa6ba39d3/master.m3u8"

# This channel labels TS segments as .jpeg; ffmpeg refuses unless we relax the
# extension check. -extension_picky 0 is the flag that actually works on 5.1.
HLS_IN = ["-extension_picky", "0", "-allowed_extensions", "ALL"]


def run(cmd, **kw):
    kw.setdefault("text", True)
    proc = subprocess.run(cmd, **kw)
    if proc.returncode != 0 and kw.get("check", True):
        raise RuntimeError("command failed: " + " ".join(cmd))
    return proc


def record(src, seconds, out):
    print(f"[record] capturing {seconds}s from source -> {out}")
    run([
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
        *HLS_IN, "-i", src,
        "-t", str(seconds), "-c", "copy", out,
    ])


def make_delayed(base, delay, out):
    """stream B = base, but starting `delay` seconds later.

    We drop the first `delay` seconds of base, so at any output timestamp t,
    B shows base[t]. Played alongside A (=base) starting now, B lags A by
    `delay`: B's content is `delay` seconds behind A. Re-encode so PTS reset
    is clean.
    """
    print(f"[prep] building delayed stream B (delay={delay}s) -> {out}")
    run([
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
        "-ss", str(delay), "-i", base,
        "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
        "-vf", "scale=1280:-2", out,
    ])


def make_streamA(base, out):
    """stream A = base, downscaled to match B's frame size for merge sanity."""
    print(f"[prep] building stream A -> {out}")
    run([
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
        "-i", base,
        "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
        "-vf", "scale=1280:-2", out,
    ])


def load_envelope(path, sr=8000, hop=0.02):
    """Decode mono audio at sr, return short-time energy envelope + frame rate.

    We align on energy envelope rather than raw waveform: shared ambience
    (crowd, whistle) dominates the envelope, while differing commentary is
    comparatively decorrelated. On real feeds this is what makes it robust.
    """
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        wav = f.name
    try:
        run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", path,
            "-ac", "1", "-ar", str(sr), "-vn", wav,
        ])
        with wave.open(wav, "rb") as w:
            n = w.getnframes()
            raw = w.readframes(n)
        sig = np.frombuffer(raw, dtype=np.int16).astype(np.float64)
    finally:
        try:
            os.unlink(wav)
        except OSError:
            pass

    if sig.size == 0:
        raise RuntimeError(f"no audio decoded from {path}")

    hop_n = max(1, int(sr * hop))
    nframes = sig.size // hop_n
    frames = sig[: nframes * hop_n].reshape(nframes, hop_n)
    env = np.sqrt((frames ** 2).mean(axis=1) + 1e-9)
    env = np.log1p(env)              # compress dynamic range
    env -= env.mean()                # zero-mean for clean correlation
    return env, 1.0 / hop


def estimate_offset(env_a, env_b, fps):
    """Return offset in seconds: how far B lags behind A (positive => B later).

    Cross-correlate via FFT. Peak lag k (in envelope frames) means env_b
    matches env_a shifted by k; convert to seconds with fps.
    """
    n = 1
    while n < env_a.size + env_b.size:
        n <<= 1
    fa = np.fft.rfft(env_a, n)
    fb = np.fft.rfft(env_b, n)
    corr = np.fft.irfft(fa * np.conj(fb), n)
    corr = np.concatenate((corr[-(env_b.size - 1):], corr[: env_a.size]))
    lags = np.arange(-(env_b.size - 1), env_a.size)
    peak = int(np.argmax(corr))
    lag_frames = lags[peak]
    # normalized peak confidence (0..1)
    conf = float(corr[peak] / (np.linalg.norm(env_a) * np.linalg.norm(env_b) + 1e-9))
    return lag_frames / fps, conf


def merge(video_a, audio_b, offset, out):
    """A video + B audio, pulling B earlier by `offset` to re-align."""
    print(f"[merge] A.video + B.audio shifted by {offset:+.3f}s -> {out}")
    if offset >= 0:
        # B lags A: trim `offset` from the front of B's audio.
        a_in = ["-i", video_a]
        b_in = ["-ss", f"{offset:.3f}", "-i", audio_b]
    else:
        # B leads A: delay B's audio.
        a_in = ["-i", video_a]
        b_in = ["-itsoffset", f"{-offset:.3f}", "-i", audio_b]
    run([
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
        *a_in, *b_in,
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "copy", "-c:a", "aac", "-shortest", out,
    ])


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", default=SRC_DEFAULT, help="source HLS url")
    ap.add_argument("--seconds", type=int, default=40, help="capture length")
    ap.add_argument("--inject-delay", type=float, default=5.0,
                    help="known delay to inject into stream B (ground truth)")
    ap.add_argument("--workdir", default="work/demo_align")
    ap.add_argument("--skip-record", action="store_true",
                    help="reuse existing base.ts in workdir")
    args = ap.parse_args()

    os.makedirs(args.workdir, exist_ok=True)
    base = os.path.join(args.workdir, "base.ts")
    a_path = os.path.join(args.workdir, "streamA.mp4")
    b_path = os.path.join(args.workdir, "streamB.mp4")
    merged = os.path.join(args.workdir, "merged.mp4")

    if not args.skip_record or not os.path.exists(base):
        record(args.src, args.seconds, base)
    else:
        print(f"[record] reusing {base}")

    make_streamA(base, a_path)
    make_delayed(base, args.inject_delay, b_path)

    print("[align] decoding audio envelopes...")
    env_a, fps = load_envelope(a_path)
    env_b, _ = load_envelope(b_path)
    offset, conf = estimate_offset(env_a, env_b, fps)

    err = offset - args.inject_delay
    print("\n==================== ALIGNMENT RESULT ====================")
    print(f"  injected delay (ground truth) : {args.inject_delay:+.3f} s")
    print(f"  recovered offset (B lags A)   : {offset:+.3f} s")
    print(f"  error                         : {err:+.3f} s")
    print(f"  envelope frame resolution     : {1000.0/fps:.0f} ms")
    print(f"  correlation confidence        : {conf:.3f}")
    print("==========================================================\n")

    merge(a_path, b_path, offset, merged)
    print(f"[done] merged output: {merged}")
    print("       A's video is now paired with B's audio, re-aligned.")

    if abs(err) <= 1.5 / fps + 0.05:
        print("\n[PASS] recovered offset matches injected delay within tolerance.")
    else:
        print("\n[WARN] recovered offset deviates from injected delay; inspect envelopes.")
        sys.exit(1)


if __name__ == "__main__":
    main()
