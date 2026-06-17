# 直播同步控制后端

本地运行：

```bash
LIVE_SYNC_STATE_DIR=/state LIVE_SYNC_HLS_DIR=/hls uvicorn app.main:app --host 0.0.0.0 --port 8000
```

配置示例：

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

接口：

- `POST /m3u/search`，请求体为 `{ "playlist_url": "...", "query": "cctv", "limit": 50 }`
- `POST /m3u/resolve`，请求体为 `{ "playlist_url": "...", "query": "cctv5" }`
- `GET /profiles`, `GET /profiles/{id}`, `PUT /profiles/{id}`, `DELETE /profiles/{id}`
- `POST /stream/start`，请求体为 `{ "profile_id": "default" }`
- `POST /stream/stop`, `GET /stream/status`, `GET /logs`
- `POST /snapshot`, `GET /snapshot/latest`
- `GET/PUT /config/{id}/offset`
- `GET/PUT /config/{id}/roi`

后端默认把 JSON 状态写入 `LIVE_SYNC_STATE_DIR`，未设置时使用 `/state`。HLS 输出默认写入 `LIVE_SYNC_HLS_DIR`，未设置时使用 `/hls`。
