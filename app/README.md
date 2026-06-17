# Live Sync Control Plane

Run locally:

```bash
LIVE_SYNC_STATE_DIR=/state LIVE_SYNC_HLS_DIR=/hls uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Profile example:

```json
{
  "video": {
    "playlist_url": "https://example.com/video.m3u",
    "primary_channel": "bbc 4k uk",
    "fallback_channels": ["fussball tv 1 4k de", "fussball tv 2 4k de"]
  },
  "audio": {
    "playlist_url": "http://example.local/audio.m3u",
    "channel": "cctv5"
  },
  "settings": {
    "segment_time": 2,
    "playlist_size": 30,
    "timeout_seconds": 20,
    "max_same_url_timeouts": 3
  }
}
```

Endpoints:

- `POST /m3u/search` with `{ "playlist_url": "...", "query": "cctv", "limit": 50 }`
- `POST /m3u/resolve` with `{ "playlist_url": "...", "query": "cctv5" }`
- `GET /profiles`, `GET /profiles/{id}`, `PUT /profiles/{id}`, `DELETE /profiles/{id}`
- `POST /stream/start` with `{ "profile_id": "default" }`
- `POST /stream/stop`, `GET /stream/status`, `GET /logs`
- `POST /snapshot`, `GET /snapshot/latest`
- `GET/PUT /config/{id}/offset`
- `GET/PUT /config/{id}/roi`

The backend stores JSON state in `LIVE_SYNC_STATE_DIR` or `/state` by default. It writes HLS output to `LIVE_SYNC_HLS_DIR` or `/hls` by default.
