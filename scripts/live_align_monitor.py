#!/usr/bin/env python3
"""Live aligner: pull two HLS feeds, measure A/B offset, merge A-video+B-audio.

Pairs with serve_two_live.py. Where demo 1 aligned offline (record then align),
this runs the live loop: every `--interval` seconds it grabs the most recent
window of audio from BOTH feeds, cross-correlates to estimate how far feed B
lags feed A, smooths the estimate, and (optionally) muxes feed A's video with
feed B's audio shifted by that offset.

Offset estimation here uses RAW-waveform cross-correlation on a mono mixdown --
demo 2 (commentary_robustness) found raw holds up better than the energy
envelope (or a band-limited version) when the two feeds carry different
commentary. Normalized peak height is reported as confidence and gates whether
we trust an update.

This is a demonstrator of the measurement loop, not a production merger:
  - it samples a short audio window each tick rather than decoding continuously
  - drift between ticks is handled by re-measuring, not by resampling
Good enough to show the offset is recovered live and tracks the ground truth.

Requires: ffmpeg/ffprobe, numpy.
"""
import argparse
import os
import subprocess
import tempfile
import threading
import time
import wave

import numpy as np

HLS_IN = ["-extension_picky", "0", "-allowed_extensions", "ALL"]
SR = 8000


def grab_audio(url, seconds, sr=SR):
    """Decode the trailing `seconds` of a live HLS feed to mono float array.

    ffmpeg on a live playlist starts near the live edge; we capture `seconds`
    of audio and return it. Returns None on failure (feed hiccup/reconnect)."""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        wav = f.name
    try:
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             *HLS_IN, "-i", url,
             "-t", str(seconds), "-ac", "1", "-ar", str(sr), "-vn", wav],
            text=True,
        )
        if proc.returncode != 0 or not os.path.exists(wav):
            return None
        with wave.open(wav, "rb") as w:
            raw = w.readframes(w.getnframes())
    except Exception:
        return None
    finally:
        try:
            os.unlink(wav)
        except OSError:
            pass
    sig = np.frombuffer(raw, dtype=np.int16).astype(np.float64)
    return sig if sig.size else None


def grab_both(url_a, url_b, seconds, sr=SR):
    """Capture both feeds' trailing windows CONCURRENTLY.

    Critical: a live `-t N` capture takes ~N seconds of wall-clock, so doing A
    then B sequentially would offset their windows by ~N seconds of real time
    and destroy the measured relationship. Both captures must start at the same
    wall-clock instant; we launch them on threads and join.
    """
    out = {}

    def worker(key, url):
        out[key] = grab_audio(url, seconds, sr)

    ta = threading.Thread(target=worker, args=("a", url_a))
    tb = threading.Thread(target=worker, args=("b", url_b))
    ta.start()
    tb.start()
    ta.join()
    tb.join()
    return out.get("a"), out.get("b")


def estimate_offset(a, b, sr=SR, max_lag=30.0):
    """Seconds by which b's content lags a's content, + normalized confidence.

    Zero-mean raw-waveform cross-correlation via FFT, restricted to +/- max_lag.
    Demo 2 (commentary_robustness) found raw correlation tracks the shared
    ambience far better than a band-limited or envelope version when the two
    feeds carry different commentary -- the broadband shared sound forms a sharp
    correlation peak, and filtering it away lets self-similar narrowband content
    pull the peak to the wrong lag. So we deliberately do NOT band-limit here.
    """
    a = a - a.mean()
    b = b - b.mean()
    n = 1
    while n < a.size + b.size:
        n <<= 1
    corr = np.fft.irfft(np.fft.rfft(a, n) * np.conj(np.fft.rfft(b, n)), n)
    corr = np.concatenate((corr[-(b.size - 1):], corr[: a.size]))
    lags = np.arange(-(b.size - 1), a.size)
    # restrict to plausible lag window
    lim = int(max_lag * sr)
    mask = np.abs(lags) <= lim
    corr_w = np.where(mask, corr, -np.inf)
    peak = int(np.argmax(corr_w))
    conf = float(corr[peak] / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9))
    return -lags[peak] / sr, conf


class Smoother:
    """Median-of-window with a confidence gate; ignores low-confidence ticks."""
    def __init__(self, window=5, min_conf=0.05):
        self.buf = []
        self.window = window
        self.min_conf = min_conf

    def update(self, offset, conf):
        if conf < self.min_conf:
            return None
        self.buf.append(offset)
        if len(self.buf) > self.window:
            self.buf.pop(0)
        return float(np.median(self.buf))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--feed-a", required=True, help="video-source HLS url")
    ap.add_argument("--feed-b", required=True, help="audio-source HLS url")
    ap.add_argument("--interval", type=float, default=10.0,
                    help="seconds between re-measurements")
    ap.add_argument("--window", type=float, default=20.0,
                    help="audio window length captured each tick (>= max expected offset)")
    ap.add_argument("--ground-truth", type=float, default=None,
                    help="known B-minus-A skew, for live error reporting")
    ap.add_argument("--ticks", type=int, default=0,
                    help="stop after N measurements (0 = run forever)")
    ap.add_argument("--min-conf", type=float, default=0.05)
    args = ap.parse_args()

    smoother = Smoother(min_conf=args.min_conf)
    print(f"{'time':>8} | {'raw off':>9} {'conf':>6} | {'smoothed':>9}"
          + ("  {:>9}".format("error") if args.ground_truth is not None else ""))
    print("-" * (40 + (12 if args.ground_truth is not None else 0)))

    start = time.time()
    tick = 0
    while True:
        t0 = time.time()
        # capture both windows concurrently (see grab_both docstring)
        a, b = grab_both(args.feed_a, args.feed_b, args.window)
        elapsed = time.time() - start

        if a is None or b is None:
            print(f"{elapsed:>7.1f}s | feed unavailable (A={'ok' if a is not None else 'X'} "
                  f"B={'ok' if b is not None else 'X'}) -- retrying")
        else:
            n = min(a.size, b.size)
            offset, conf = estimate_offset(a[:n], b[:n])
            smoothed = smoother.update(offset, conf)
            line = f"{elapsed:>7.1f}s | {offset:>+8.3f}s {conf:>6.3f} | "
            line += f"{smoothed:>+8.3f}s" if smoothed is not None else f"{'(gated)':>9}"
            if args.ground_truth is not None and smoothed is not None:
                line += f"  {smoothed - args.ground_truth:>+8.3f}s"
            print(line, flush=True)

        tick += 1
        if args.ticks and tick >= args.ticks:
            break
        sleep = args.interval - (time.time() - t0)
        if sleep > 0:
            time.sleep(sleep)

    if smoother.buf:
        final = float(np.median(smoother.buf))
        print(f"\nfinal smoothed offset: {final:+.3f}s")
        if args.ground_truth is not None:
            print(f"ground truth         : {args.ground_truth:+.3f}s")
            print(f"final error          : {final - args.ground_truth:+.3f}s")


if __name__ == "__main__":
    main()
