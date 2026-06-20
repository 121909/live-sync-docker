# Dual-feed alignment demo

Proof-of-concept scripts for aligning two live feeds of the same match that
carry different commentary, then merging A-video + B-audio. Built on branch
`demo/manual-delay-align`. All scripts are stdlib + ffmpeg + numpy.

## The scripts

| script | what it does |
|---|---|
| `demo_manual_delay_align.py` | Offline proof: record from one channel, manufacture a delayed copy with a known offset, recover it from audio, merge. Validates the measure+merge path. |
| `demo_commentary_robustness.py` | Stress test: simulates two feeds (shared ambience + *different* commentary, independent encodes) and sweeps commentary loudness to see where offset estimation breaks. Finding: raw-waveform cross-correlation holds far better than the energy envelope. |
| `serve_two_live.py` | Publishes two local video files as rolling HLS "live" feeds (real-time pacing, PROGRAM-DATE-TIME) on one HTTP port, with a controllable A/B skew = ground truth. |
| `live_align_monitor.py` | Pulls both live feeds, measures the B-vs-A offset continuously, smooths it with a confidence gate. The live measurement loop. |

## Running the real two-feed test

You provide two recordings of the same match with different commentary.

1. Publish them as two live feeds (here B's content starts 8s ahead = ground truth):

   ```bash
   python3 scripts/serve_two_live.py feedA.mp4 feedB.mp4 --skew 8 --port 8800
   ```

   Prints:
   ```
   Feed A : http://<host>:8800/A/master.m3u8
   Feed B : http://<host>:8800/B/master.m3u8
   ```

2. From another shell, run the live aligner against those URLs:

   ```bash
   python3 scripts/live_align_monitor.py \
     --feed-a http://<host>:8800/A/master.m3u8 \
     --feed-b http://<host>:8800/B/master.m3u8 \
     --interval 8 --window 22 --ground-truth -8.0
   ```

   It prints the recovered offset and (if `--ground-truth` is given) the live
   error each tick. With the synthetic rig it locks the injected skew to within
   the sample period.

If your two files already differ in commentary timing, set `--skew 0` and let
the aligner discover the real offset between them; drop `--ground-truth`.

## What's proven vs. still open

Proven on this rig:
- offset recovered exactly when audio is shared (demo 1) and live (monitor)
- raw cross-correlation survives different commentary far better than envelope
  in simulation (demo 2)
- concurrent capture is mandatory; band-limiting the audio breaks the estimate

Still open (needs your real feeds):
- whether raw correlation holds under *real lossy codecs* on each feed, not the
  gentle additive-noise proxy demo 2 uses
- the merge step under live drift (the monitor measures; it does not yet mux a
  continuous re-aligned output — that's the next build once offset tracking is
  confirmed on real material)
- ad-break / commercial handling (different ads per feed) — see the design
  discussion; not yet implemented here
