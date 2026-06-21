# 自动对齐优化方案

## 目标

当前自动对齐流程已经具备基本链路：

1. 从缓存前视频/音频抓同步帧
2. OCR 读比赛时间得到候选 offset
3. 构造验证链路复核候选
4. 验证通过后 handoff 到新链路

但从当前代码和运行证据看，实际问题不是单一 OCR 精度，而是自动对齐前后半段都偏脆，导致系统经常停留在 `capture_failed` 或高频反复探测，难以稳定完成首次对齐和后续维护。

本方案的目标不是小调参数，而是把自动对齐改造成“低误触发、高成功率、可诊断、可渐进上线”的流程。

## 当前证据

### 运行证据

- `work/monitor_18081_20260621T000718Z.report.md`
  - 在 `2026-06-21T00:07:18Z` 到 `2026-06-21T00:37:18Z` 的 30 分钟窗口内，`auto_align_state` 多次进入 `capture_failed`
  - 该窗口内服务还伴随 source cache 故障和重启，说明自动对齐需要更强的抗抖动能力

### 代码证据

- [app/auto_align.py](/root/live-sync-docker/app/auto_align.py)
  - 当前状态只有 `waiting/probing/verifying/aligned/disabled/capture_failed`
  - 还没有真正实现 README 中描述的 `prematch_low_freq / locked_low_freq / reacquire`

- [app/server.py](/root/live-sync-docker/app/server.py:2435)
  - 每轮探测固定抓 3 对帧
  - 只要这一轮拿不到有效读数，就按整轮失败处理

- [app/server.py](/root/live-sync-docker/app/server.py:2713)
  - OCR ROI 基本固定在顶部四分之一
  - 对不同联赛、不同计时器布局适应性不足

- [app/server.py](/root/live-sync-docker/app/server.py:2745)
  - 验证阶段仍然需要再抓 3 对帧
  - 验证成本高，且对抓帧稳定性继续放大

- [app/server.py](/root/live-sync-docker/app/server.py:2216)
  - handoff 依赖新的 HLS writer 预热成功
  - 一旦预热失败，自动对齐虽然不一定打断当前直播，但对齐本身无法完成

## 根因拆解

### 1. 抓帧阶段过脆

当前探测模型是“固定抓 3 次，整轮判断”。这对滚动 HLS 的边缘片段、上游偶发超时、4K 源抖动都不够鲁棒。

结果是：

- 单次 ffmpeg 抓帧失败会直接拖累整轮
- 短时不同步会被放大成完整 `capture_failed`
- 系统更容易进入退避，而不是真正积累到有效样本

### 2. 候选阶段缺少本地预筛

README 已经定义了赛前低频、本地疑似计时器判断、高频候选、锁定后低频巡检，但当前实现并没有这一套真正落地。

结果是：

- 没有计时器、广告、回放时也会进入真实候选探测
- 无效 OCR 请求偏多
- 自动对齐触发时机不够干净

### 3. OCR 只做“固定 ROI + 多 provider fallback”，缺少位置适应

当前 OCR 的增强主要是：

- 多 provider fallback
- RapidOCR 图像变体

但 ROI 本身没有真正动态化。

结果是：

- 转播台标、比分牌位置变化时，候选阶段会长期不稳定
- 同一个联赛切不同信号源时，成功率可能明显变化

### 4. 验证和切换成本偏高

现在候选和验证都依赖实际抓同步帧，验证通过后还要完整 handoff。

结果是：

- 前半段一旦不稳，后半段很难走到
- 即便候选正确，也可能死在验证抓帧或 handoff 预热

### 5. 可观测性不够

当前 WebUI 和状态字段能看到：

- `state`
- `message`
- `video_clock/audio_clock`
- `mismatch_count`

但看不到关键细节：

- 本轮到底是抓帧失败、skew 过大、OCR 不稳还是验证失败
- 本轮候选 offset 列表是什么
- 验证阶段每个 delta 是多少

这会让调优停留在猜测层。

## 优化目标

建议把自动对齐优化分成三层目标。

### A. 稳定性目标

- 探测阶段不再频繁进入 `capture_failed`
- 单次上游抖动不应立即导致整轮失败
- 已对齐状态下，广告/回放/计时器消失不应触发无意义切换

### B. 成功率目标

- 首次出现计时器后，能够在有限时间内完成首次对齐
- 同一场比赛中，后续漂移修正应更少依赖高频 OCR
- 不同台标/计时器布局下成功率更接近

### C. 可诊断目标

- 每次失败都能明确归类
- 每轮候选和验证都能回看关键数值
- 监控报告能直接汇总失败分布，而不是只看状态切换

## 分阶段优化方案

## 第一阶段：先止血

这一阶段不改整体架构，目标是尽快把成功率和可观测性拉起来。

### 1. 探测改为“限时收集有效样本”，而不是“固定 3 次硬判”

修改位置：

- [app/server.py](/root/live-sync-docker/app/server.py:2435)
- [app/auto_align.py](/root/live-sync-docker/app/auto_align.py:320)

建议方案：

- 一轮候选最多尝试 5 到 6 次抓帧
- 只要收集到 2 个有效样本，就提前结束候选分析
- 如果 6 次里只拿到 1 个有效样本，记为 `ocr_unstable`
- 如果 6 次里 0 个有效样本，再记为 `capture_failed`

这样能把“偶发坏样本”和“整轮完全不可用”区分开。

### 2. 失败类型细分

修改位置：

- [app/auto_align.py](/root/live-sync-docker/app/auto_align.py:294)
- [app/server.py](/root/live-sync-docker/app/server.py:2412)

建议新增失败分类：

- `source_unavailable`
- `capture_failed`
- `capture_skew`
- `ocr_unstable`
- `candidate_rejected`
- `verify_failed`
- `handoff_failed`

状态可以继续复用现有主状态，但 monitor 内必须明确保存 `last_failure_reason`。

### 3. 增加结构化 monitor 字段

修改位置：

- [app/auto_align.py](/root/live-sync-docker/app/auto_align.py:49)
- [app/server.py](/root/live-sync-docker/app/server.py:2412)
- [app/static/app.js](/root/live-sync-docker/app/static/app.js:264)

建议新增字段：

- `last_failure_reason`
- `last_capture_error`
- `candidate_offsets`
- `candidate_cluster_size`
- `verify_deltas`
- `capture_skew_ms`
- `probe_attempts`
- `valid_samples`

UI 不需要一次性全部展示，但后端必须先产出这些数据。

### 4. 候选阶段增加最小连续性检查

修改位置：

- [app/auto_align.py](/root/live-sync-docker/app/auto_align.py:219)

除了现在的 cluster 逻辑，额外加两条约束：

- 样本中的比赛时间必须基本单调推进
- 候选值相对当前 offset 变化过大时，需要更强证据，比如至少 3 个样本一致

这样可以过滤广告回放、补时牌和偶发 OCR 异常。

## 第二阶段：补齐真正的状态机

这一阶段让实现对齐 README 中原本设想的链路。

### 1. 落地真实状态机

建议状态：

- `prematch_low_freq`
- `candidate`
- `verify`
- `locked_low_freq`
- `reacquire`
- `disabled`

对应行为：

- `prematch_low_freq`
  - 只做低频本地检查
  - 尽量不打远程 OCR

- `candidate`
  - 检测到疑似计时器后，升频抓样本
  - 计算候选 offset

- `verify`
  - 只在候选满足要求时进入

- `locked_low_freq`
  - 首次对齐后降频维护
  - 不再按首次对齐时的高频模式工作

- `reacquire`
  - 仅在明确漂移或计时器恢复时回到高频

修改位置：

- [app/auto_align.py](/root/live-sync-docker/app/auto_align.py)
- [app/server.py](/root/live-sync-docker/app/server.py:2912)

### 2. 加入本地“疑似计时器存在”门控

建议实现：

- 在 `prematch_low_freq` 下只做轻量本地判断
- 判断方式可以是：
  - RapidOCR 本地结果命中时间格式
  - 固定 ROI 内亮度/边缘/文字密度特征
  - 最近成功 ROI 的局部重复检测

触发规则建议是：

- 连续 2 轮本地判断为疑似有计时器，才进入 `candidate`

### 3. 锁定后降频巡检

当前成功一次后只是 `aligned`，但并没有真正进入维护态。

建议：

- 首次对齐成功后，巡检频率降低到正常探测频率的 3 到 5 倍
- 只有连续多轮发现明显偏移，才进入 `reacquire`
- 中场、广告、回放时保持当前 offset，不触发切换

## 第三阶段：提高跨源鲁棒性

### 1. ROI 记忆 + 多 ROI 搜索

修改位置：

- [app/server.py](/root/live-sync-docker/app/server.py:2713)
- [app/server.py](/root/live-sync-docker/app/server.py:3656)

建议策略：

1. 优先使用“最近一次成功 ROI”
2. 失败后回退到联赛/频道的候选 ROI 列表
3. 成功后更新最近成功 ROI
4. 将 ROI 记忆持久化到 `state/profile.json` 或独立状态文件

这比永远只扫顶部四分之一更实用。

### 2. 按场景调整验证成本

修改位置：

- [app/server.py](/root/live-sync-docker/app/server.py:2745)

建议：

- 小幅 offset 变化时，验证样本数可以少于首次对齐
- 与最近一次成功 offset 接近时，允许更轻量验证
- 大幅 offset 变化时，维持更严格验证

目标是把验证成本与风险挂钩，而不是所有情况都同样重。

### 3. 优化 handoff 风险

修改位置：

- [app/server.py](/root/live-sync-docker/app/server.py:2216)

建议方向：

- 把 handoff 失败从“对齐结果失败”与“直播输出异常风险”进一步解耦
- 记录 handoff 预热耗时、首段生成耗时、失败阶段
- 后续可以考虑小 offset 变更时使用更轻量的切换路径

这里不建议第一阶段就重写，但必须先把诊断打全。

## 建议的实施顺序

### 第 1 批

- 探测重试模型改造
- 失败类型细分
- 结构化 monitor 字段

这一批收益最高、风险最低，应优先做。

### 第 2 批

- 本地计时器门控
- 真正的低频/高频状态切换
- 锁定后低频维护

这一批决定自动对齐是否会“正常触发、正常休眠、必要时再唤醒”。

### 第 3 批

- ROI 记忆
- 多 ROI 搜索
- 验证成本分级

这一批决定跨联赛、跨源、跨包装的稳定性上限。

### 第 4 批

- handoff 路径性能与可靠性优化

这部分更适合作为单独专题，避免在前半段还不稳时过早复杂化。

## 验收指标

优化后建议至少观察以下指标。

### 自动对齐成功率

- 首次出现计时器后，在限定时间内完成首次对齐的比例
- 对齐成功后 30 分钟内无需重新获取的比例

### 探测稳定性

- `capture_failed` 占全部 probe 的比例
- `ocr_unstable` 占全部 probe 的比例
- 候选阶段平均有效样本数

### OCR 成本

- 首次成功对齐平均消耗的 OCR 请求次数
- 已锁定状态下单位时间 OCR 请求数

### handoff 健康度

- handoff 成功率
- 预热首段耗时
- handoff 失败分类分布

## 监控与回归建议

建议后续新增一份自动对齐专项监控报告，至少包含：

- 状态停留时间分布
- 失败原因 Top N
- 候选 offset 分布
- 验证 delta 分布
- 首次成功时间
- 首次成功前 OCR 请求数

这样优化时不会只盯着“有没有成功”，而是能看到瓶颈从哪一层移动。

## 建议的近期落地范围

如果只做一轮最小但有效的优化，建议范围控制在：

1. 改候选抓样本逻辑
2. 细分失败原因
3. 扩展 monitor/status 字段
4. 前端增加诊断展示

这 4 项做完后，再跑一次长时间监控，才能判断问题主要还剩在：

- 抓帧
- OCR
- 验证
- handoff

否则现在的可观测性不足以支撑更大改造。
