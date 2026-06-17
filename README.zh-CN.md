# 直播同步管理

这个项目用于把一路高画质视频和一路中文解说音频合成为本地 HLS 直播流，方便在浏览器、VLC 或 Emby 中观看。典型用途是保留 4K 画面，同时使用中文解说。

## 特点

- 视频和音频分开选择，只取音频源的音频，不使用它的低清画面。
- 支持 M3U 频道列表，按频道名选择主视频源、备用视频源和音频源。
- 视频源连续失败后会刷新 M3U；链接变化时使用新链接，链接未变时切换到备用频道。
- 自动对齐默认开启，通过 OCR 读取两路画面里的比赛计时器来计算 offset。
- 没有计时器时不会重新对齐，会沿用上一次可靠 offset。
- 连续多次检测到稳定不一致后才切换 offset，避免回放画面或识别误差造成误调。
- 新 offset 会通过 HLS handoff 接上，尽量减少播放器中断。
- 支持每日赛程自动启停，也可以在 WebUI 手动启动和停止。
- WebUI 中填写的源 URL、本地 M3U 内容和请求头会保存到服务器 `state/profile.json`，容器重启、`AUTO_START` 和赛程自动启动才能自动运行。
- 输出 `index.m3u8` 和 Emby 可用的 `emby.m3u`。

## 运行方式

构建并启动：

```bash
cd /root/live-sync-docker
docker compose up -d --build
```

Docker 镜像默认使用 Debian 12 仓库里的 `ffmpeg` 包。之前静态版 FFmpeg 在部分代理 HLS 源上可能出现 `code -11` 段错误，因此已改为发行版 FFmpeg。

打开 WebUI：

```text
http://<服务器IP>:18080/
```

如果在本机访问：

```text
http://127.0.0.1:18080/
```

停止：

```bash
cd /root/live-sync-docker
docker compose down
```

查看日志：

```bash
docker logs -f live-sync-cctv
```

## WebUI 使用方法

1. 在“频道选择”里填写视频 M3U 地址，可以每行一个；也可以直接粘贴“本地视频 M3U 内容”，点击“刷新视频频道”。
2. 用过滤框搜索频道，选中后点击“设为主源”。
3. 如果需要备用源，选中其他频道后点击“加入备用”。
4. 填写音频 M3U 地址，可以每行一个；也可以直接粘贴“本地音频 M3U 内容”，点击“刷新音频频道”。
5. 选中中文解说频道，点击“设为音频”。
6. 在“基础设置”里确认初始偏移秒、自动对齐、HLS 分片等参数。
7. 点击“保存”，再点击“启动”。
8. 用“工具 / 截图”里的同步截图检查计时器位置。程序运行中截图来自当前播放链路（包含 offset 延迟后的输入），不是原始未延迟源。
9. 复制 HLS 地址，或在 Emby 中使用 `emby.m3u`。

## 常用参数

- `初始偏移秒`：启动时使用的 offset。正数表示延迟视频，负数表示延迟音频。
- `低请求本地缓存（不转码）`：默认开启。每路上游只由一个常驻 ffmpeg 连接读取，并用 `copy` 写成本地滚动 HLS；合并、截图和自动 OCR 都读本地缓存，避免重复请求直播地址。
- `本地缓存秒数`：默认 240 秒。实际保留时间会至少覆盖当前 offset 和少量缓冲。
- `自动对齐（OCR）`：开启后每次自动截图完成后立即读取两路计时器并计算 offset。
- `对齐一次后暂停自动截图和检查`：确认对齐后停止后续自动截图和 OCR 检查，直播继续运行；手动同步截图仍可使用。
- `非比赛时间直播也自动对齐`：默认关闭。关闭时，如果赛程已刷新且当前不在比赛窗口，手动启动直播也只合并输出，不做自动截图和 OCR 对齐；开启后，非比赛时间手动直播也会继续自动对齐。
- `截图/检查间隔（秒）`：多久自动截图一次；截图保存后会立即用同一批帧做自动对齐检查。
- `连续不一致次数`：连续多少次发现稳定偏差后才真正调整。
- `允许误差（秒）`：候选 offset 和当前 offset 差值小于该值时认为已对齐。
- `最大偏移（秒）`：超过该范围的 OCR 结果会被丢弃。
- `视频计时器区域` / `音频计时器区域`：格式为 `x,y,w,h`，数值为 0 到 1 的相对坐标。
- `视频计时器预设区域` / `音频计时器预设区域`：每行一个 `x,y,w,h`。适合给同一方源配置多个常见计时器位置，例如不同频道、主直播画面、备用转播画面。

默认内置以下计时器预设区域：

```text
视频：
0.132,0.055,0.078,0.140
0.333,0.058,0.080,0.140
0.114,0.049,0.077,0.077
0.111,0.000,0.077,0.185

音频：
0.824,0.080,0.078,0.140
```

- `分片时长`：HLS 每个 ts 分片的目标时长，默认不小于 4 秒。
- `播放列表分片数`：播放器可回看的分片数量，窗口约等于 `分片时长 * 分片数`。

## 自动对齐流程

程序运行后会按“截图/检查间隔”从视频源缓存和音频源缓存各抓一帧，保存截图后立即用同一批帧检查计时器。计时器位置查找顺序是：

1. 先尝试当前配置/已锁定的 `视频计时器区域` 或 `音频计时器区域`。
2. 如果当前区域读不到，再按对应的预设 ROI 列表逐个尝试。
3. 如果预设也读不到，才扫描画面上三分之一区域自动寻找计时器位置。

预设 ROI 命中后会锁定为当前 ROI；自动扫描命中后也会保存为当前 ROI。换频道或管线重建后，会重新按这个顺序寻找。

读到计时器后会换算为比赛秒数，例如：

- `51:20` 解析为第 3080 秒。
- `45:00+02:13` 解析为第 2833 秒。
- 补时显示在下方一行时，也会尝试合并识别。

候选 offset 的计算方式是：

```text
offset = -(音频源比赛时间 - 视频源比赛时间)
```

例如中文源计时器比 4K 画面慢 25 秒，候选 offset 就是 `+25`，程序会延迟视频来等待中文音频。

如果没有计时器、只有一边读到计时器、或 OCR 结果超过最大 offset，程序不会调整，会继续沿用现有 offset。只有连续多次得到稳定的新 offset，才会预热新 HLS 管线并切换。

如果开启“对齐一次后暂停自动截图和检查”，程序在确认已对齐或完成一次 offset handoff 后，会停止后续自动截图和自动 OCR 检查，降低 CPU 占用。直播输出和手动截图不受影响。

## 输出格式

默认按源视频编码选择输出格式：HEVC/HDR/Dolby Vision 源使用 fMP4（分片 MP4），普通 H264 源自动使用 MPEG-TS 兼容模式。这样可以避免 H264 源被错误套用 HEVC/Dolby Vision 参数。

默认视频、音频和本地缓存都使用 `copy`，不主动转码。需要为了播放器兼容性把输出音频转成 AAC 时，可以设置 `OUTPUT_AUDIO_CODEC=aac` 后重建容器。

如果某个播放器或 Emby 不支持 fMP4 HLS，可以通过环境变量回退到 MPEG-TS：

```bash
HLS_SEGMENT_TYPE=mpegts docker compose up -d --build
```

或在 `docker-compose.yml` 的 `environment` 里加：

```yaml
HLS_SEGMENT_TYPE: "${HLS_SEGMENT_TYPE:-mpegts}"
```

## 视频闪烁

Fussball 频道在不支持 HDR 的设备上可能会闪烁。如果只在非 HDR 电视、手机或浏览器里出现闪烁，优先怀疑设备或播放器的 HDR/HLG/Dolby Vision 兼容性。

4K 源出现短暂闪烁或花屏，通常是源里 Dolby Vision RPU 元数据异常或 HEVC PPS/NAL 解析错误导致。日志里出现以下内容是源端问题，不是本程序造成的：

```
[hevc] Error parsing NAL unit
[hevc] Multiple Dolby Vision RPUs found in one AU. Skipping previous.
```

程序在输入侧已添加 `-fflags +discardcorrupt` 丢弃损坏包。探测到 HEVC 源时会启用 fMP4 输出，并默认用 `filter_units=remove_types=62` 移除 Dolby Vision RPU 附加 NAL，以降低解析出错和闪烁概率；H264 源不会使用这些 HEVC 专用参数。如果源本身不稳定，这些措施无法彻底消除闪烁。需要保留 Dolby Vision 元数据时，可以设置 `STRIP_DOVI_RPU=0` 后重建容器。

保留 Dolby Vision 元数据的回退命令：

```bash
STRIP_DOVI_RPU=0 docker compose up -d --build
```

## 自动对齐改进

两边比赛计时器数值完全一致时，会立即应用新的 offset，不再等待多次确认。只有两边不一致时仍然需要连续多次确认。

## Emby 使用

在 Emby 后台添加 Live TV M3U tuner：

```text
http://<服务器IP>:18080/emby.m3u
```

可选 XMLTV：

```text
http://<服务器IP>:18080/guide.xml
```

如果 Emby 运行在另一台机器或另一个容器里，确保它能访问：

```text
http://<服务器IP>:18080/index.m3u8
```

4K HEVC 最好让 Emby Direct Play。如果 Emby 触发转码，CPU/GPU 占用会很高，也更容易卡顿。

## 隐私和存储

服务器会保存这些配置：

- 视频 M3U 地址
- 音频 M3U 地址
- 本地视频 M3U 内容
- 本地音频 M3U 内容
- 视频/音频请求头
- 频道名
- fallback 列表
- offset
- ROI
- HLS 参数
- 自动对齐参数
- 赛程开关和时间窗口

这些源配置会写入 `state/profile.json`。这是自动运行所必需的：如果不保存，容器重启或赛程自动启动时会因为没有视频 M3U 而报 `no video M3U configured`。

不要把 `state/profile.json` 公开或提交到公共仓库，因为里面可能包含源 URL、请求头、Cookie 或本地 M3U 内容。

为了方便排错，运行日志默认会显示实际 URL，包括 ffmpeg 打开的输入地址和源站报错地址。如果需要日志隐藏 URL，可以设置：

```bash
LOG_REDACT_URLS=1 docker compose up -d --build
```

## 目录说明

- `app/server.py`：WebUI 和实时管线主程序。
- `app/static/`：WebUI 页面、脚本和样式。
- `state/profile.json`：WebUI 保存的配置，包括 M3U 源、请求头、频道名、offset、ROI、HLS 参数和自动对齐参数。
- `state/last_sync_offset.json`：最近一次可靠 offset。
- `state/snapshots/`：视频/音频截图。
- `hls/`：生成的 HLS 播放列表和 ts 分片。
- `data/work/source_cache/`：低请求模式下的本地滚动源缓存，运行时自动创建和清理。
- `docker-compose.yml`：容器运行配置。

## 排错

如果播放器偶尔卡顿，先检查：

```bash
docker logs --tail=200 live-sync-cctv
```

重点看是否频繁出现源超时、切换频道、ffmpeg 退出、自动对齐 handoff 失败。

如果日志出现上游分片 `HTTP error 403 Forbidden`，程序会在后面输出 `upstream context ...`，其中 `input0` 是视频输入，`input1` 是音频输入。这样可以判断是视频源还是音频源被上游拒绝。程序默认给 ffmpeg 使用 Emby 风格请求头：

```text
User-Agent: Emby
Accept: */*
```

如果某个源要求特定 UA，可以设置：

```bash
FFMPEG_USER_AGENT="你的 User-Agent" docker compose up -d --build
```

如果要设置全局默认请求头，可以设置：

```bash
DEFAULT_REQUEST_HEADERS=$'User-Agent: Emby\nAccept: */*\nCache-Control: no-cache\nPragma: no-cache' docker compose up -d --build
```

播放器请求 `/index.m3u8` 时如果短暂返回 503，通常表示管线正在重启或还没生成新播放列表；这不是播放器访问被拒绝。

如果 OCR 占用 CPU 高，可以适当调大“截图/检查间隔（秒）”。

如果对齐不准，先用“视频截图”和“音频截图”确认两边 ROI 是否覆盖计时器，再调整 ROI。
