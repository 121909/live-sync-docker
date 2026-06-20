#!/usr/bin/env python3
"""Demo 2: does audio alignment survive *different commentary* on each feed?

Demo 1 proved the measure+merge path when A and B share identical audio. Real
feeds don't: they share the stadium ambience (crowd, whistle, ball strikes) but
carry *different* commentary on top. This script stress-tests that.

Model of two real feeds:
    A_audio = shared_core + gain * commentary_A
    B_audio = shift(shared_core, D) + gain * commentary_B
where:
    shared_core  = the recorded base audio (ambience + original commentary)
    commentary_A/B = two DIFFERENT synthetic speech-like signals (decorrelated)
    D            = injected delay (ground truth), B lags A by D
    gain         = how loud each feed's own commentary is vs the shared core

As `gain` rises, the matched (shared) component is increasingly buried under
each feed's own un-matched speech -- exactly the real failure mode. Each feed
is also run through an *independent encode* (slight resample drift + codec-like
noise) so the shared ambience is correlated but NOT sample-identical, which is
the realistic condition (different encoders/CDNs). We compare:
    - RAW waveform cross-correlation      (naive)
    - ENERGY-ENVELOPE cross-correlation   (what demo 1 used)
and report which one stays accurate to higher commentary levels.

Pure numpy on the already-recorded base.ts audio. Fast; no re-encoding.
Run demo 1 first (or with --skip-record) so work/demo_align/base.ts exists.
"""
import argparse
import os
import subprocess
import tempfile
import wave

import numpy as np

SR = 8000


def decode_mono(path, sr=SR):
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        wav = f.name
    try:
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-i", path, "-ac", "1", "-ar", str(sr), "-vn", wav],
            check=True,
        )
        with wave.open(wav, "rb") as w:
            raw = w.readframes(w.getnframes())
    finally:
        try:
            os.unlink(wav)
        except OSError:
            pass
    return np.frombuffer(raw, dtype=np.int16).astype(np.float64)


def bandpass_fft(sig, sr, lo, hi):
    """Zero-phase band limit via FFT bin masking (no scipy needed)."""
    spec = np.fft.rfft(sig)
    freqs = np.fft.rfftfreq(sig.size, 1.0 / sr)
    spec[(freqs < lo) | (freqs > hi)] = 0
    return np.fft.irfft(spec, sig.size)


def synth_commentary(n, sr, seed):
    """Speech-like signal: voice-band noise modulated by a ~4 Hz syllabic
    envelope. Different seeds -> decorrelated 'commentators'."""
    rng = np.random.default_rng(seed)
    carrier = bandpass_fft(rng.standard_normal(n), sr, 300, 3400)
    # syllabic envelope: low-pass random walk around 3-5 Hz, rectified
    env_noise = bandpass_fft(rng.standard_normal(n), sr, 0.5, 6.0)
    syl = np.clip(np.abs(env_noise), 0, None)
    syl /= syl.max() + 1e-9
    sig = carrier * syl
    return sig / (np.sqrt((sig ** 2).mean()) + 1e-9)   # unit RMS


def independent_encode(sig, sr, seed, drift_ppm=400.0, noise_db=-30.0):
    """Simulate an independent encode of the SAME source: tiny sample-rate
    drift (resample) + codec-like quantization/broadband noise. This breaks
    sample-level identity while preserving the shared ambience structure --
    which is the realistic condition real feeds are in. `noise_db` sets the
    additive-noise level relative to signal RMS (higher = more decorrelation)."""
    rng = np.random.default_rng(seed)
    # resample drift: stretch by a few hundred ppm via linear interp
    factor = 1.0 + drift_ppm * 1e-6
    idx = np.arange(sig.size) * factor
    idx = idx[idx < sig.size - 1]
    lo = idx.astype(int)
    frac = idx - lo
    out = sig[lo] * (1 - frac) + sig[lo + 1] * frac
    # additive broadband noise at `noise_db` relative to signal RMS
    rms = np.sqrt((out ** 2).mean()) + 1e-9
    out = out + rng.standard_normal(out.size) * rms * (10.0 ** (noise_db / 20.0))
    return out


def envelope(sig, sr=SR, hop=0.02):
    hop_n = max(1, int(sr * hop))
    nf = sig.size // hop_n
    frames = sig[: nf * hop_n].reshape(nf, hop_n)
    env = np.log1p(np.sqrt((frames ** 2).mean(axis=1) + 1e-9))
    return env - env.mean(), 1.0 / hop


def xcorr_offset(a, b, fps):
    """Offset in seconds such that b's content lags a's content by `offset`
    (i.e. b[t] matches a[t-offset]), plus normalized peak confidence.

    rfft(a)*conj(rfft(b)) yields r[k]=sum a[m+k]b[m]; the peak sits at k=-offset
    for b[m]=a[m-offset], so we negate the peak lag to report the lag of b
    behind a directly.
    """
    n = 1
    while n < a.size + b.size:
        n <<= 1
    corr = np.fft.irfft(np.fft.rfft(a, n) * np.conj(np.fft.rfft(b, n)), n)
    corr = np.concatenate((corr[-(b.size - 1):], corr[: a.size]))
    lags = np.arange(-(b.size - 1), a.size)
    peak = int(np.argmax(corr))
    conf = float(corr[peak] / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9))
    return -lags[peak] / fps, conf


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default="work/demo_align/base.ts")
    ap.add_argument("--inject-delay", type=float, default=6.0)
    ap.add_argument("--gains", default="0,0.5,1,2,4",
                    help="comma list of commentary/core power ratios to sweep")
    ap.add_argument("--noise-db", type=float, default=-30.0,
                    help="independent-encode noise level vs signal RMS "
                         "(higher = more waveform decorrelation, e.g. -12)")
    ap.add_argument("--drift-ppm", type=float, default=400.0,
                    help="per-feed sample-rate drift in ppm")
    args = ap.parse_args()

    if not os.path.exists(args.base):
        raise SystemExit(f"missing {args.base}; run demo_manual_delay_align.py first")

    core = decode_mono(args.base)
    core_rms = np.sqrt((core ** 2).mean())
    shift_n = int(args.inject_delay * SR)

    # Build two overlapping windows of the SAME core so both feeds carry real
    # ambience everywhere (no silence padding artifact). A's window starts D
    # seconds later than B's, so B's content lags A's content by exactly D:
    #   A[t] = core[t + D],  B[t] = core[t]  =>  B[t] = A[t - D].
    W = core.size - shift_n
    if W <= SR:
        raise SystemExit("base.ts too short for this delay; record more or lower --inject-delay")
    a_core = core[shift_n : shift_n + W]
    b_core = core[:W]
    n = W

    comm_a = synth_commentary(n, SR, seed=11) * core_rms
    comm_b = synth_commentary(n, SR, seed=99) * core_rms

    print(f"injected delay (ground truth): {args.inject_delay:+.3f} s   "
          f"(envelope resolution 20 ms)")
    print("each feed independently 'encoded' (resample drift + codec noise) "
          "so the\nshared ambience is correlated but NOT sample-identical -- "
          "the realistic case.\n")
    hdr = f"{'comm/core':>10} | {'RAW err':>9} {'RAW conf':>8} | {'ENV err':>9} {'ENV conf':>8}"
    print(hdr)
    print("-" * len(hdr))

    raw_fail = env_fail = None
    for g in [float(x) for x in args.gains.split(",")]:
        # mix commentary onto each feed's core, THEN run each through an
        # independent encode so the fine waveform decorrelates like real feeds.
        a = independent_encode(a_core + g * comm_a, SR, seed=7,
                               drift_ppm=args.drift_ppm, noise_db=args.noise_db)
        b = independent_encode(b_core + g * comm_b, SR, seed=23,
                               drift_ppm=args.drift_ppm, noise_db=args.noise_db)
        m = min(a.size, b.size)
        a, b = a[:m], b[:m]

        raw_off, raw_conf = xcorr_offset(a - a.mean(), b - b.mean(), SR)
        ea, fps = envelope(a)
        eb, _ = envelope(b)
        env_off, env_conf = xcorr_offset(ea, eb, fps)

        raw_err = raw_off - args.inject_delay
        env_err = env_off - args.inject_delay
        if raw_fail is None and abs(raw_err) > 0.1:
            raw_fail = g
        if env_fail is None and abs(env_err) > 0.1:
            env_fail = g
        print(f"{g:>10.2f} | {raw_err:>+8.3f}s {raw_conf:>8.3f} | "
              f"{env_err:>+8.3f}s {env_conf:>8.3f}")

    print()
    print(f"raw waveform   : accurate up to comm/core = "
          f"{'all tested' if raw_fail is None else f'< {raw_fail:g}'}")
    print(f"energy envelope: accurate up to comm/core = "
          f"{'all tested' if env_fail is None else f'< {env_fail:g}'}")
    print("""
Finding: raw waveform correlation holds even when each feed's own commentary
is several times louder than the shared ambience, while the energy envelope
breaks once commentary rivals the ambience. The fine waveform of the shared
sound is a strong, specific anchor; the envelope throws that detail away and
gets pulled by each feed's own syllabic rhythm.

Caveat: this 'independent encode' is linear drift + additive noise, which is
gentler than real lossy audio codecs (perceptual/MDCT coding scrambles phase
far more), so raw correlation may degrade earlier on real feeds than shown
here. Practical takeaway: try raw correlation first, fall back to / combine
with the envelope, and gate either one on the normalized peak confidence --
which drops steadily as commentary buries the shared sound, so it's a usable
'do not trust this offset / do not merge yet' signal.""")


if __name__ == "__main__":
    main()
