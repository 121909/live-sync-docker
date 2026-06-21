# 直播同步管理

把一路视频源和一路音频源合成为本地 HLS 直播流，适合“保留高码率画面 + 使用另一条解说音频”的场景。当前正式服务入口只有 `app/server.py`，推荐通过 Docker Compose 运行。

## 当前能力

- WebUI 管理视频源、音频源、请求头、偏移、HLS 参数和赛程开关。
- 支持远程 M3U 和直接粘贴本地 M3U 内容。
- 视频和音频都支持主频道 + fallback 频道。
- 默认启用本地源缓存：上游各维持一个常驻 ffmpeg 读取，输出、截图和自动对齐都读本地缓存。
- 支持正负偏移。
  - 正数：延迟视频。
  - 负数：延迟音频。
- 支持自动截图、OCR 识别比赛计时器、自动校正 offset。
- 支持按赛程自动启停直播。
- 支持录制当前输出，并在停止后合并为单个 `mkv` 文件。
- 提供 HLS、Emby M3U、简单 XMLTV 和 `strm` 辅助接口。

## 快速启动

```bash
cd /root/live-sync-docker
docker compose up -d --build
```

打开 WebUI：

```text
http://<服务器IP>:18080/
```

本机访问：

```text
http://127.0.0.1:18080/
```

查看日志：

```bash
docker logs -f live-sync-cctv
```

停止：

```bash
docker compose down
```

宿主机监听地址和端口可覆盖：

```bash
HOST_BIND=127.0.0.1 PORT=18081 docker compose up -d --build
```

## 部署说明

- 镜像基于 `debian:12-slim`。
- 运行依赖由镜像内安装，不依赖宿主机 `.venv/`。
- 主服务监听容器内 `18080`。
- Compose 默认挂载：
  - `./state:/state`
  - `./hls:/hls`
- 启动命令由 `scripts/docker-entrypoint.sh` 拉起 `sshd` 后再执行 `python3 -m app.server`。

## WebUI 使用

1. 填写视频 M3U 地址，或直接粘贴“本地视频 M3U 内容”。
2. 刷新视频频道并选中要使用的频道。
   第 1 个为主源，其余按顺序作为 fallback。
3. 填写音频 M3U 地址，或直接粘贴“本地音频 M3U 内容”。
4. 刷新音频频道并选中要使用的频道。
   第 1 个为主音频，其余按顺序作为 fallback。
5. 按需要设置 `offset_seconds`、本地缓存、自动对齐、赛程和 HLS 参数。
6. 点击“保存”。
7. 点击“启动”或“重启”。

WebUI 中的主要工具：

- `同步截图`
  抓取当前四个截图槽位：`cache_video`、`cache_audio`、`video`、`audio`。
- `测试 OCR 服务`
  用内置测试图验证当前 OCR 配置。
- `开始录制` / `停止录制`
  保存当前输出 HLS，并可导出单个 `mkv`。
- `刷新赛程`
  立即重新拉取赛程。
- `清理 HLS`
  清空 `hls/` 输出。
- `清理状态`
  清理 `state/` 下的运行时状态，但保留 `profile.json`、`last_sync_offset.json` 和录制目录。

## 输出地址

默认输出：

- HLS: `/index.m3u8`
- Emby M3U: `/emby.m3u`
- XMLTV: `/guide.xml`
- STRM: `/cctv5.strm`

示例：

```text
http://<服务器IP>:18080/index.m3u8
http://<服务器IP>:18080/emby.m3u
http://<服务器IP>:18080/guide.xml
```

`PUBLIC_BASE_URL` 已设置时，`/emby.m3u` 和 `/cctv5.strm` 会使用该地址生成外部播放 URL。

## 自动对齐

当前实现以 `app/auto_align.py` + `app/server.py` 为准，行为是：

- 自动对齐依赖本地源缓存；关闭本地缓存时，自动对齐会暂停。
- 自动对齐默认只在比赛窗口内工作。
  - 如果关闭赛程功能，则始终允许。
  - 如果开启 `auto_align_debug_override`，则忽略比赛窗口限制。
- 每轮流程是：
  - 从本地缓存抓取视频/音频探测帧
  - OCR 识别两边计时器
  - 计算候选 offset
  - 验证候选值
  - 验证通过后热切换到新 offset
- 自动对齐状态会显示在 WebUI 的 `auto_align_state` 和监控面板中。

当前支持的 OCR 路径：

- `OCR.space`
- 自定义 OpenAI 兼容接口
- `RapidOCR` 本地兜底

自动对齐是否可用，不只取决于 WebUI 选择的 OCR 服务商；只要本地 `RapidOCR` 可用，或远端 OCR 配置完整，就会启用相关能力。

## 赛程自动启停

当前仅支持 `espn` 作为赛程提供方。

赛程相关行为：

- 启动时会启动后台调度线程。
- 调度线程按轮询周期刷新赛程并判断当前是否处于比赛窗口。
- 在比赛窗口内，如果当前没有运行且没有手动阻止，会自动启动直播。
- 如果直播是由赛程线程启动的，离开比赛窗口后会自动停止。
- 手动停止会在当前比赛窗口内生效，直到这场比赛窗口结束后才恢复自动接管。

## 录制

录制直接镜像当前输出 HLS，不会额外请求原始上游。

当前录制行为：

- 录制目录：`state/recordings/<session_id>/`
- 保存播放列表和当前分片副本
- 停止后可在 WebUI 中执行合并
- 当前只支持导出 `mkv`

## 环境变量

常用变量如下。除非特别说明，WebUI 保存的配置会覆盖运行时配置。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `HOST_BIND` | `0.0.0.0` | 宿主机绑定地址，仅 Compose 端口映射使用。 |
| `PORT` | `18080` | 宿主机暴露端口；容器内服务仍监听 `18080`。 |
| `AUTO_START` | `0` | 容器启动后是否自动启动直播。 |
| `DEFAULT_OFFSET` | `10` | 无保存状态时的默认 offset。 |
| `SYNC_OFFSET` | 空 | 启动时强制使用该 offset。 |
| `VIDEO_M3U_URL` | 空 | 默认视频 M3U 地址，支持多行。 |
| `AUDIO_M3U_URL` | 空 | 默认音频 M3U 地址，支持多行。 |
| `VIDEO_CHANNEL_NAME` | 空 | 默认视频主频道名。 |
| `AUDIO_CHANNEL_NAME` | 空 | 默认音频主频道名。 |
| `FALLBACK_VIDEO_CHANNELS` | 空 | 默认视频 fallback 列表。 |
| `FALLBACK_AUDIO_CHANNELS` | 空 | 默认音频 fallback 列表。 |
| `VIDEO_HEADERS` | 空 | 默认视频请求头，多行 `Header: value`。 |
| `AUDIO_HEADERS` | 空 | 默认音频请求头，多行 `Header: value`。 |
| `LOCAL_CACHE_ENABLED` | `1` | 是否启用本地源缓存。 |
| `LOCAL_CACHE_SECONDS` | `360` | 本地源缓存目标深度。 |
| `SEGMENT_TIME` | `4` | HLS 分片时长。 |
| `PLAYLIST_SIZE` | `60` | HLS 播放列表保留分片数。 |
| `HLS_SEGMENT_TYPE` | `auto` | HLS 分片类型。当前 `auto` 最终会走 `mpegts`。 |
| `STRIP_DOVI_RPU` | `1` | HEVC copy 时是否移除 Dolby Vision RPU 附加 NAL。 |
| `OUTPUT_AUDIO_CODEC` | `copy` | 输出音频编码，当前非 `copy` 时回落为 `aac`。 |
| `PUBLIC_BASE_URL` | 空 | 生成对外播放地址时使用的基础 URL。 |
| `CHANNEL_ID` | `cctv5-4k-cn` | `/emby.m3u` 和 `/guide.xml` 使用的频道 ID。 |
| `CHANNEL_NAME` | `CCTV5 4K Chinese` | 默认输出频道名。 |
| `CHANNEL_NUMBER` | `5` | `/emby.m3u` 使用的频道号。 |
| `CHANNEL_GROUP` | `Sports` | `/emby.m3u` 使用的频道分组。 |
| `AUTO_ALIGN_INTERVAL` | `60` | 自动截图 / 自动对齐检查周期。 |
| `AUTO_ALIGN_THRESHOLD` | `1` | 候选 offset 的最小变化阈值。 |
| `AUTO_ALIGN_MAX_OFFSET` | `180` | 允许的最大 offset。 |
| `OCR_PROVIDER` | 空 | 优先 OCR 服务商，`ocrspace` 或 `custom`。 |
| `OCRSPACE_API_KEY` | 空 | OCR.space API Key。 |
| `OCR_API_KEY` | 空 | 自定义 OCR API Key。 |
| `OCR_CUSTOM_ENDPOINT` | 空 | 自定义 OCR 端点。 |
| `OCR_CUSTOM_MODEL` | `gpt-4o` | 自定义 OCR 模型名。 |
| `FFMPEG_USER_AGENT` | `Emby` | 默认 ffmpeg User-Agent。 |
| `DEFAULT_REQUEST_HEADERS` | 空 | ffmpeg 默认请求头，多行 `Header: value`。 |
| `LOG_REDACT_URLS` | `0` | 是否在日志中隐藏 URL。 |
| `SCHEDULE_ENABLED` | `1` | 是否启用赛程自动启停。 |
| `SCHEDULE_PROVIDER` | `espn` | 赛程提供方，当前仅支持 `espn`。 |
| `SCHEDULE_LEAGUE` | `fifa.world` | ESPN 赛程 league。 |
| `SCHEDULE_TIMEZONE` | `Asia/Shanghai` | 赛程展示和窗口计算时区。 |
| `SCHEDULE_REFRESH_HOURS` | `24` | 赛程刷新缓存周期。 |
| `SCHEDULE_POLL_SECONDS` | `60` | 调度轮询周期。 |
| `SCHEDULE_PRE_MINUTES` | `10` | 比赛前提前启动分钟数。 |
| `SCHEDULE_DURATION_MINUTES` | `150` | 比赛主体窗口分钟数。 |
| `SCHEDULE_POST_MINUTES` | `20` | 比赛后额外保留分钟数。 |
| `SSHD_ENABLED` | `1` | 是否在容器内启动 sshd。 |
| `SSHD_USER` | `root` | sshd 用户名。 |
| `SSHD_PASSWORD` | `live-sync` | sshd 密码。 |

示例：

```bash
AUTO_START=1 LOCAL_CACHE_SECONDS=300 docker compose up -d --build
```

```bash
DEFAULT_REQUEST_HEADERS=$'User-Agent: Emby\nAccept: */*\nCache-Control: no-cache\nPragma: no-cache' docker compose up -d --build
```

## SSH 调试

镜像默认启动 `sshd`。

当前 Compose 配置会把容器 `22` 端口映射到宿主机 `172.17.0.1:2222`，并且同时 `expose 22` 供同一 Docker 网络内其他容器访问。

默认账号取决于 Compose 环境变量：

```text
用户：root
密码：live-sync
```

关闭或覆盖：

```bash
SSHD_ENABLED=0 docker compose up -d --build
SSHD_USER=debug SSHD_PASSWORD='change-me' docker compose up -d --build
```

## 目录说明

| 路径 | 说明 |
| --- | --- |
| `app/server.py` | 主服务与 Web API。 |
| `app/auto_align.py` | 自动对齐控制器。 |
| `app/static/` | WebUI 静态资源。 |
| `configs/ocr/rapidocr_timer.yaml` | RapidOCR 配置。 |
| `state/profile.json` | WebUI 保存的配置。 |
| `state/last_sync_offset.json` | 最近一次保存的 offset。 |
| `state/snapshots/` | 截图输出。 |
| `state/recordings/` | 录制与合并输出。 |
| `hls/` | 当前直播 HLS 输出。 |
| `scripts/docker-entrypoint.sh` | 容器入口脚本。 |

## 辅助脚本

仓库里还保留了一组与主服务独立的 8800 辅助上游工具：

- `scripts/serve_two_live.py`
- `scripts/restart_two_live_upstream.py`
- `scripts/two_live_upstream_scheduler.sh`
- `configs/two_live_upstream.json`

这组脚本用于把本地素材发布成两个滚动 HLS 上游，方便本地测试，不属于正式 WebUI 主链路。

## 排错

先看日志：

```bash
docker logs --tail=200 live-sync-cctv
```

常见问题：

- `HTTP error 403` 或源站拒绝
  优先检查视频/音频请求头、UA、Cookie、Referer。
- `/index.m3u8` 短暂返回 `503`
  一般表示管线正在启动、重启或切换，不一定是故障。
- 自动对齐长期为 `disabled`
  先确认本地缓存没有关闭，再检查 OCR 是否可用，以及当前是否处于比赛窗口。
- 录制无法合并
  当前只支持导出 `mkv`。
- 上游分片持续不更新
  重点看 ffmpeg stderr、源缓存是否退出、以及 `timeout_seconds` 与上游稳定性。

## 隐私与本地产物

`state/profile.json` 可能包含：

- 源 URL
- 请求头
- Cookie
- 本地粘贴的 M3U 内容

不要把这些内容直接公开或提交到仓库。

仓库当前还包含一些本地运行产物，例如 `.venv/`、`work/`、`hls/`、`core.*`、`testvideo/`。这些不属于服务说明的一部分，按本地环境自行管理。
