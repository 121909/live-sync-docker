# 直播同步管理

把一路高画质视频和一路中文解说音频合成为本地 HLS 直播流。典型用途是保留 4K 画面，同时使用中文解说，并输出给浏览器、VLC 或 Emby 播放。

## 主要能力

- 视频源和音频源分开选择，输出只使用主视频源画面和音频源声音。
- 支持远程 M3U 和本地粘贴 M3U，按频道名选择主视频、备用视频和音频频道。
- 默认启用低请求本地缓存：每路上游由一个常驻 ffmpeg 连接读取，截图、OCR 和合并都读本地缓存。
- 默认不转码，视频、音频和源缓存都使用 `copy`。
- 自动 OCR 读取两路画面里的比赛计时器，计算并切换 offset。
- 自动截图和自动对齐使用同一个周期：截图保存后立即用同一批帧做 OCR 检查。
- 视频源失败后会刷新 M3U；链接变化时使用新链接，链接未变时切换备用频道。
- 支持每日赛程自动启停，也可以在 WebUI 手动启动和停止。
- 支持录制当前输出，停止后可选择是否合并为单文件。
- 输出 `/index.m3u8` 和 Emby 可用的 `/emby.m3u`。

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

默认容器内端口是 `18080`。宿主机绑定地址和端口可以通过 `HOST_BIND`、`PORT` 调整，例如：

```bash
HOST_BIND=127.0.0.1 PORT=18081 docker compose up -d --build
```

## 容器内网 SSH 测试

镜像默认启动 `sshd`，只在 Docker Compose 内部网络声明 `22` 端口，不映射到宿主机。默认测试账号：

```text
用户：debug
密码：live-sync
```

可以通过环境变量调整或关闭：

```bash
SSHD_USER=tester SSHD_PASSWORD='change-me' docker compose up -d --build
SSHD_ENABLED=0 docker compose up -d --build
```

同一 Docker 网络里的容器可以用服务名访问：

```bash
ssh debug@live-sync
```

## WebUI 使用流程

1. 在“频道选择”里填写视频 M3U 地址，或粘贴“本地视频 M3U 内容”。
2. 点击“刷新视频频道”，筛选并选中频道，点击“设为主源”。
3. 如需备用源，选中其他视频频道后点击“加入备用”。
4. 填写音频 M3U 地址，或粘贴“本地音频 M3U 内容”。
5. 点击“刷新音频频道”，筛选并选中中文解说频道，点击“设为音频”。
6. 在“基础设置”里确认 offset、低请求本地缓存、自动对齐、ROI 和 HLS 参数。
7. 点击“保存”，再点击“启动”。
8. 用“工具 / 截图”里的“同步截图”检查两路计时器位置。
9. 复制 HLS 地址，或在 Emby 中使用 `/emby.m3u`。
10. 需要留档时，点“开始录制”，停止后可在录制列表里选择是否合并成单文件。

WebUI 中填写的 M3U、请求头、频道名、offset、ROI 和自动对齐配置会保存到 `state/profile.json`。容器重启、`AUTO_START` 和赛程自动启动都依赖这份配置。

## 请求模型

默认 `LOCAL_CACHE_ENABLED=1`。启动后程序会先为视频源和音频源各启动一个本地滚动缓存：

```text
上游视频源 -> 本地 video_cache.m3u8
上游音频源 -> 本地 audio_cache.m3u8
```

后续合并、截图和自动 OCR 都读取本地缓存，不再为截图或对齐额外请求直播地址。播放器访问的是本地 `/index.m3u8` 和本地分片，也不会直接请求上游。

如果关闭低请求缓存，程序会回到直接读取上游的方式，截图和 OCR 可能产生额外短请求。

## 自动对齐

程序按“截图/检查间隔”抓取视频源缓存和音频源缓存各一帧，先保存截图，再立即用同一批帧做 OCR 检查。

计时器位置查找顺序：

1. 先尝试当前配置或已锁定的 `视频计时器区域` / `音频计时器区域`。
2. 读不到时，按对应的预设 ROI 列表逐个尝试。
3. 预设也读不到时，扫描画面上三分之一区域自动寻找计时器。

预设 ROI 或自动扫描命中后会锁定为当前 ROI。换频道或管线重建后，会重新按这个顺序寻找。

计时器会换算为比赛秒数：

- `51:20` 解析为第 3080 秒。
- `45:00+02:13` 解析为第 2833 秒。
- 补时显示在下方一行时，也会尝试合并识别。

候选 offset 的计算方式：

```text
offset = -(音频源比赛时间 - 视频源比赛时间)
```

例如中文源计时器比 4K 画面慢 25 秒，候选 offset 就是 `+25`，程序会延迟视频来等待中文音频。

如果没有计时器、只有一边读到计时器、或 OCR 结果超过最大 offset，程序不会调整，会继续沿用当前 offset。只有连续多次得到稳定的新 offset，才会预热新 HLS 管线并切换。两边计时器完全一致时，会立即应用新的 offset。

默认内置计时器预设区域：

```text
视频：
0.132,0.055,0.078,0.140
0.333,0.058,0.080,0.140
0.114,0.049,0.077,0.077
0.111,0.000,0.077,0.185

音频：
0.824,0.080,0.078,0.140
```

## 常用参数

| WebUI 参数 | 说明 |
| --- | --- |
| `初始偏移秒` | 启动时使用的 offset。正数表示延迟视频，负数表示延迟音频。 |
| `低请求本地缓存（不转码）` | 默认开启。每路上游只由一个常驻 ffmpeg 连接读取，并用 `copy` 写成本地滚动 HLS。 |
| `本地缓存秒数` | 默认 360 秒。实际保留时间会至少覆盖当前 offset 和少量缓冲。 |
| `截图/检查间隔（秒）` | 自动截图周期；截图保存后会立即用同一批帧做自动对齐检查。 |
| `连续不一致次数` | 连续多少次发现稳定偏差后才真正调整。 |
| `允许误差（秒）` | 候选 offset 和当前 offset 差值小于该值时认为已对齐。 |
| `最大偏移（秒）` | 超过该范围的 OCR 结果会被丢弃。 |
| `OCR 服务商` | 仅支持第三方 OCR。配置可用服务商后，自动对齐自动开启；未配置则关闭。 |
| `OCR.space API Key` / `自定义 OCR API Key` / `自定义端点 URL` / `模型名称` | 第三方 OCR 服务配置。OCR.space fallback 使用独立的 OCR.space Key。 |
| `测试 OCR 服务` | 立即校验当前 OCR 服务商配置是否可用。 |
| `分片时长` | HLS 每个分片的目标时长，默认不小于 4 秒。 |
| `播放列表分片数` | 播放器可回看的分片数量，窗口约等于 `分片时长 * 分片数`。 |


## OCR 计时器识别

默认使用第三方 OCR 识别计时器。当前支持 [OCR.space](https://ocr.space) 和自定义 OpenAI 兼容接口。

注册地址：https://ocr.space/ocrapi （免费用户每月 25,000 次请求）

### 配置方式

1. 选择 OCR 服务商。
2. 填写对应服务商的 API Key；如果是自定义服务商，还需要填写端点 URL，可选模型名称。
3. 点击“测试 OCR 服务”确认配置可用。
4. 保存后，若 OCR 服务商配置有效，自动对齐会自动开启；未配置则自动关闭。

如果不配置可用的 OCR 服务商，自动对齐会关闭，不影响直播输出。

### 工作方式

未锁定计时器位置时，服务会对截图做智能扫描：

1. 发送画面上半部分给 OCR 服务 → 找到计时器文本和位置。
2. 根据文本在左半还是右半，下次只发送对应的四分之一区域。
3. 逐次缩小范围，快速定位计时器位置。

锁定 ROI 后，只发送 ROI 区域给 OCR 服务，节省请求量。

### 对齐频率

- 未对齐前：每 15 秒检查一次（快速收敛）。
- 对齐后：每 5 分钟检查一次（低频率，保持稳定）。
- 未锁定 ROI：每 30 秒全图扫描。

## 环境变量

常用环境变量可以在启动命令前设置，也可以写入 `docker-compose.yml` 的 `environment`。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `AUTO_START` | `0` | 容器启动后是否自动启动直播。 |
| `DEFAULT_OFFSET` | `10` | 没有保存 offset 时的默认值。 |
| `SYNC_OFFSET` | 空 | 强制指定启动 offset。 |
| `LOCAL_CACHE_ENABLED` | `1` | 是否启用低请求本地缓存。 |
| `LOCAL_CACHE_SECONDS` | `360` | 本地源缓存秒数。 |
| `AUTO_ALIGN_INTERVAL` | `60` | 自动截图和检查周期。 |
| `AUTO_ALIGN_SAMPLES` | `3` | 连续确认次数。 |
| `AUTO_ALIGN_THRESHOLD` | `1` | offset 允许误差秒数。 |
| `AUTO_ALIGN_MAX_OFFSET` | `180` | 最大允许 offset。 |
| `OCR_PROVIDER` | 空 | OCR 服务商，支持 `ocrspace` 或 `custom`。 |
| `OCR_API_KEY` | 空 | 自定义 OCR 服务的 API Key。 |
| `OCRSPACE_API_KEY` | 空 | OCR.space 专用 API Key。 |
| `OCR_CUSTOM_ENDPOINT` | 空 | 自定义 OCR 服务端点，需为 OpenAI 兼容接口。 |
| `OCR_CUSTOM_MODEL` | `gpt-4o` | 自定义 OCR 服务使用的模型名。 |
| `OUTPUT_AUDIO_CODEC` | `copy` | 输出音频编码固定为 `copy`，不转码。 |
| `HLS_SEGMENT_TYPE` | `auto` | 输出 HLS 分片类型。`auto` 按频道名判断：带 `4k` 使用 fMP4，否则使用 MPEG-TS。 |
| `STRIP_DOVI_RPU` | `1` | HEVC 源是否移除 Dolby Vision RPU 附加 NAL。 |
| `FFMPEG_USER_AGENT` | `Emby` | 默认 ffmpeg User-Agent。 |
| `DEFAULT_REQUEST_HEADERS` | 空 | 全局默认请求头，多行 `Header: value`。 |
| `LOG_REDACT_URLS` | `0` | 是否在日志中隐藏 URL。 |
| `PUBLIC_BASE_URL` | 空 | 对外公开的基础 URL，用于生成播放地址。 |

录制文件保存在 `state/recordings/<session_id>/` 下，停止后可通过 WebUI 选择输出 `MKV` 或 `MP4`。

示例：

```bash
AUTO_START=1 LOCAL_CACHE_SECONDS=300 docker compose up -d --build
```

设置全局请求头：

```bash
DEFAULT_REQUEST_HEADERS=$'User-Agent: Emby\nAccept: */*\nCache-Control: no-cache\nPragma: no-cache' docker compose up -d --build
```

## 输出格式和播放

默认按频道名选择输出格式：

- 频道名包含 `4k` 时使用 fMP4。
- 其它频道使用 MPEG-TS。

默认视频、音频和本地缓存都使用 `copy`，不主动转码。多音轨源会优先选择 AAC 音轨并直接复制。

如果某个播放器或 Emby 不支持 fMP4 HLS，可以回退到 MPEG-TS：

```bash
HLS_SEGMENT_TYPE=mpegts docker compose up -d --build
```

Emby 添加 Live TV M3U tuner：

```text
http://<服务器IP>:18080/emby.m3u
```

可选 XMLTV：

```text
http://<服务器IP>:18080/guide.xml
```

如果 Emby 在另一台机器或另一个容器里，确保它能访问：

```text
http://<服务器IP>:18080/index.m3u8
```

4K HEVC 最好让 Emby Direct Play。如果 Emby 触发转码，CPU/GPU 占用会很高，也更容易卡顿。

## 目录和隐私

运行时会写入这些目录：

| 路径 | 说明 |
| --- | --- |
| `state/profile.json` | WebUI 保存的配置，包括 M3U、请求头、频道名、offset、ROI 和自动对齐参数。 |
| `state/last_sync_offset.json` | 最近一次可靠 offset。 |
| `state/snapshots/` | 视频和音频截图。 |
| `hls/` | 输出 HLS 播放列表和分片。 |
| `data/work/source_cache/` 或容器内 `WORK_DIR/source_cache/` | 低请求模式下的本地滚动源缓存，运行时自动创建和清理。 |

不要公开或提交 `state/profile.json`，里面可能包含源 URL、请求头、Cookie 或本地 M3U 内容。

日志默认会显示实际 URL，包括 ffmpeg 打开的输入地址和源站报错地址。需要隐藏 URL 时：

```bash
LOG_REDACT_URLS=1 docker compose up -d --build
```

## 排错

先看最近日志：

```bash
docker logs --tail=200 live-sync-cctv
```

重点关注源超时、频道切换、ffmpeg 退出、自动对齐 handoff 失败和上游 403。

### 上游 403

如果日志出现 `HTTP error 403 Forbidden`，程序会输出 `upstream context ...`。其中 `input0` 是视频输入，`input1` 是音频输入，可以判断是哪一路被上游拒绝。

默认 ffmpeg 请求头：

```text
User-Agent: Emby
Accept: */*
Cache-Control: no-cache
Pragma: no-cache
```

如果源要求特定 UA：

```bash
FFMPEG_USER_AGENT="你的 User-Agent" docker compose up -d --build
```

如果源要求额外请求头，优先在 WebUI 的视频/音频请求头里填写，格式为每行一个 `Header: value`。

### 播放器短暂 503

播放器请求 `/index.m3u8` 时如果短暂返回 503，通常表示管线正在启动、重启或还没生成新播放列表，不代表播放器访问被拒绝。

### OCR 不准

先用“同步截图”确认两边 ROI 是否覆盖计时器，再调整 `视频计时器区域`、`音频计时器区域` 或预设 ROI。

如果 OCR 占用 CPU 高，可以调大“截图/检查间隔（秒）”。

### 视频闪烁

如果只在非 HDR 电视、手机或浏览器里出现闪烁，优先怀疑设备或播放器的 HDR/HLG/Dolby Vision 兼容性。

4K 源出现短暂闪烁或花屏，通常是源里 Dolby Vision RPU 元数据异常或 HEVC PPS/NAL 解析错误导致。日志里出现以下内容多半是源端问题：

```text
[hevc] Error parsing NAL unit
[hevc] Multiple Dolby Vision RPUs found in one AU. Skipping previous.
```

程序默认会在探测到 HEVC 源时移除 Dolby Vision RPU 附加 NAL，以降低解析出错和闪烁概率。需要保留 Dolby Vision 元数据时：

```bash
STRIP_DOVI_RPU=0 docker compose up -d --build
```
