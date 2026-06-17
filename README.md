# Live Sync CCTV

Build and run:

```bash
cd /root/live-sync-docker
docker compose up --build
```

Open:

```text
http://127.0.0.1:18080/
```

The Web UI lets you configure playlist URLs, local M3U content, request headers, select video/audio channels by name, order fallback video channels, start/stop the live output, capture screenshots, and inspect the process log. Source configuration is written to `./state/profile.json` so container restarts, `AUTO_START`, and schedule automation can run without a browser session.

The container starts in configuration mode by default. Click `Start` in the Web UI after confirming the sources. To auto-start on container boot:

```bash
AUTO_START=1 docker compose up --build
```

The HLS output is still available at:

```text
http://127.0.0.1:18080/index.m3u8
```

If watching from another machine, replace `127.0.0.1` with the Docker host IP.

Manual offset:

```bash
SYNC_OFFSET=30 docker compose up --build
```

Resolve channels from M3U playlists:

```bash
VIDEO_M3U_URL="http://example/video.m3u" \
VIDEO_CHANNEL_NAME="Main 4K" \
FALLBACK_VIDEO_CHANNELS="Backup 4K,Backup HD" \
AUDIO_M3U_URL="http://example/audio.m3u" \
AUDIO_CHANNEL_NAME="Chinese commentary" \
docker compose up --build
```

If you only have direct stream URLs, paste them into the Web UI as local M3U content:

```m3u
#EXTM3U
#EXTINF:-1 group-title="Local",BBC Stream 41 UHD
https://ve-uhd-push-uk.live.fastly.md.bbci.co.uk/x=4/i=urn:bbc:pips:service:uk_bbc_stream_041/iptv_uhd_v1.mpd
#EXTINF:-1 group-title="Local",CCTV5
http://host.docker.internal:<port>/<path>.m3u8
```

State is stored in `./state`. HLS files are written to `./hls`. Source URLs, local M3U content, and request headers entered in the Web UI are saved in `./state/profile.json`; do not publish that file if it contains private URLs, cookies, or tokens.
Logs show URLs by default for debugging.
The Web UI clear actions remove the contents of those directories without removing
the mountpoints. `./state/last_sync_offset.json` keeps the last reliable offset.
Auto-align is enabled by default. While the live pipeline is running, the manager
periodically grabs one frame from each source (`AUTO_ALIGN_INTERVAL`, default 60
seconds). It does not record an alignment clip. When both locked timer ROIs can
be read, it compares the game clocks and only realigns after
`AUTO_ALIGN_SAMPLES` consecutive mismatches (default 3). The saved offset is
updated and handed off to a new ffmpeg writer in the same HLS playlist with
`EXT-X-DISCONTINUITY`. The Python server does not restart, and old HLS segments
are kept long enough for clients to continue reading. When no timer is visible,
or either locked ROI cannot be read, it keeps the previous offset.
Stoppage-time clocks such as `45:00+02:13` and `90+03:20` are interpreted as
continuous match time. This also works when the main clock is on one line and
the added time is shown underneath it, such as `45:00` above `+02:13`.

At the start of each pipeline, the monitor uses the configured timer ROI if it
is readable; otherwise it grabs a single frame and scans the full frame to find
the timer. After a timer ROI is locked, that source stays locked until the
pipeline is recreated because a source failed or a fallback channel was selected.
If goal replays, celebration shots, half-time screens, or other no-clock frames
are shown, the monitor keeps the current offset instead of chasing replay
graphics.

Snapshots are also refreshed automatically while the live pipeline is running
(`SNAPSHOT_INTERVAL`, default 180 seconds). If OCR can read the configured timer
ROI, the saved image is only the timer crop. If no timer is found, the saved
image is the full frame.

Disable auto-align if needed:

```bash
AUTO_ALIGN_ENABLED=0 docker compose up --build
```

The compose file defaults to `DEFAULT_OFFSET=29` only when no state file exists.

The live playlist keeps about `PLAYLIST_SIZE * SEGMENT_TIME` seconds available. Defaults are `30 * 4 = 120s`.

## Match schedule automation

The server can fetch the FIFA World Cup schedule daily and start or stop the
live pipeline around match windows. It uses ESPN's public soccer scoreboard by
default:

```bash
SCHEDULE_ENABLED=1
SCHEDULE_PROVIDER=espn
SCHEDULE_LEAGUE=fifa.world
SCHEDULE_TIMEZONE=Asia/Shanghai
SCHEDULE_PRE_MINUTES=10
SCHEDULE_DURATION_MINUTES=150
SCHEDULE_POST_MINUTES=20
```

Manual Web UI controls still work. A manual start is treated as user-controlled
and will not be stopped just because no match is active. A manual stop during a
scheduled match suppresses automatic restart until that match window ends.

## Emby

Use Emby's Live TV M3U tuner. In Emby Server Dashboard:

1. Go to `Live TV`
2. Add a TV source
3. Select `M3U`
4. Use this M3U URL:

```text
http://<docker-host-ip>:18080/emby.m3u
```

Optional XMLTV guide URL. Add it under Live TV guide providers as `Xml TV`:

```text
http://<docker-host-ip>:18080/guide.xml
```

If Emby is running on another machine or inside another container, set `PUBLIC_BASE_URL` so the generated M3U contains a reachable stream URL:

```bash
PUBLIC_BASE_URL="http://<docker-host-ip>:18080" docker compose up --build
```

The generated M3U points Emby to:

```text
http://<docker-host-ip>:18080/index.m3u8
```

Channel metadata can be changed with:

```bash
CHANNEL_NAME="CCTV5 Chinese 4K" CHANNEL_NUMBER=5 CHANNEL_GROUP=Sports docker compose up --build
```

Backup option: create a `.strm` item in an Emby library using:

```text
http://<docker-host-ip>:18080/cctv5.strm
```

Live TV M3U tuner is the recommended path.
