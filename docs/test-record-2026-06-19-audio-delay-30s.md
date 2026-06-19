# 2026-06-19 Audio Delay 30s Test Record

## Result

Status: failed

The pipeline could switch the audio input to an independent local delayed source and apply `offset_seconds=30.0`, but the actual HLS handoff did not complete. The new mux warmup failed twice with:

`warmup failed: no HLS segment created after handoff within 45s`

The service was restored to the original stable configuration after the test.

## Goal

Verify the real-world path where video and audio do not come from the same upstream:

- make the audio side effectively lag by about 30 seconds
- set manual offset to `+30s`
- do the test without restarting `python3 app/server.py`
- only rebuild the runtime pipeline if needed

## Constraint

At test time, the configured M3U sources only exposed one real `FS1 01` URL:

- `http://89.187.179.148:826/anto.j/c9yJDcXyPe/120160`

There was no second real `FS1 01` upstream available for audio. Because of that, the test used an equivalent simulation:

1. create an independent local audio upstream from the same source
2. make that local audio upstream lag by about 30 seconds
3. point `audio_local_m3u` to that delayed local source
4. set `offset_seconds=30.0`

This is valid for testing the independent-audio-source mux and handoff path, but it does not cover network behavior of a true remote alternate upstream.

## Test Setup

First attempt:

- exposed delayed audio as `http://127.0.0.1:18082/audio_delay_src.m3u8`
- failed for test-harness reasons because the helper assets were under `work/`, and `WORK_DIR` is cleared during pipeline restart

Second attempt:

- moved helper assets outside `work/`
- used local file input instead of local HTTP
- audio source used by the runtime became:
  - `/root/live-sync-docker/manual_audio_upstream/audio_delay_src.m3u8`

## Key Timeline

All times below are UTC on `2026-06-19`.

- `10:52:39` profile updated for test:
  - `audio_local_m3u` pointed to local delayed audio source
  - `offset_seconds=30.0`
- `10:52:44` live pipeline stopped and restarted
- `10:52:45` audio source changed to:
  - `/root/live-sync-docker/manual_audio_upstream/audio_delay_src.m3u8`
- `10:52:53` local caches became ready
- `10:52:53` video delay pipeline started
- `10:53:23` mux warmup started:

```text
input0=local video delay HLS (video_delay.m3u8, source=/root/live-sync-docker/work/source_cache/video_cache.m3u8, offset +30.000s)
input1=local cache audio (/root/live-sync-docker/work/source_cache/audio_cache.m3u8)
output=run_004579.m3u8
```

- `10:54:13` first failure:

```text
pipeline failed for FS1 01: warmup failed: no HLS segment created after handoff within 45s
```

- `10:54:18` runtime retried automatically
- `10:54:23` second mux warmup attempt started with the same topology
- test was then stopped and rolled back
- `10:55:17` restore profile saved
- `10:55:22` pipeline restarted with original config
- `10:55:32` local cache ready again on original same-upstream path
- `10:55:56` original stream handoff published successfully:

```text
handoff: publishing warmed startup playlist run_005580.m3u8
```

## Observed Behavior

What worked:

- runtime accepted an independent audio source
- `audio_url` switched from the live HTTP URL to the local delayed file source
- `offset_seconds` changed to `30.0`
- `audio-cache` for the delayed local source started successfully
- the old HLS output kept serving during failed warmup, so the user was not immediately kicked off playback

What failed:

- the delayed-video plus independent-audio mux could not produce the new warmed HLS playlist
- `run_004579.m3u8` was never published
- the runtime reported warmup timeout instead of successful handoff

## Important Distinction

The first HTTP-based helper attempt failed because the helper files were under `work/` and got deleted by normal runtime cleanup. That was a test harness mistake.

The second local-file-based attempt removed that variable and still failed at the same product stage:

- independent audio source selected
- local caches built
- delayed video built
- mux warmup failed before handoff

So the underlying product issue remains real.

## OCR Evidence During Test

The manual snapshot taken during the delayed-source test showed a large divergence between the delayed-path video snapshot and the old audio snapshot state:

- `cache_video`: `93:49+7`
- `video`: `93:49+7`
- stale previous `audio` state still showed `89:45`

This is consistent with the test setup trying to create a roughly 30-second lagged audio path before applying video delay.

## Conclusion

Current behavior is:

- same-upstream video/audio: works
- manual delay on same-upstream test path: works
- independent delayed audio source plus `offset_seconds=30.0`: handoff warmup fails

The failure point is in the delayed-video plus independent-audio handoff path, not in OCR availability and not in basic source selection.

## Post-Test Restore State

The service was restored to original settings:

- `audio_local_m3u=""`
- `offset_seconds=0.0`
- `audio_url` restored to:
  - `http://89.187.179.148:826/anto.j/c9yJDcXyPe/120160`

Final observed healthy state after restore:

- `running=true`
- `stage=running`
- `active_channel=FS1 01`
- `active_audio_channel=FS1 01`
- `auto_align_state=aligned`
- `auto_align_msg=stable current=0.000s candidate=0.000s v=96:55+7 a=96:55+7`

## Next Focus

Recommended next debugging target:

- inspect why `video_delay.m3u8 + audio_cache.m3u8` can start the mux process but fail to produce the warmed handoff playlist when audio comes from an independent local HLS source
