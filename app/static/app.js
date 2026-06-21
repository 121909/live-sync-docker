const form = document.querySelector("#configForm");
const toast = document.querySelector("#toast");
const viewTitle = document.querySelector("#viewTitle");
const navLinks = Array.from(document.querySelectorAll("[data-view-target]"));
const views = Array.from(document.querySelectorAll("[data-view]"));
const profileFields = [
  "name",
  "video_headers",
  "audio_headers",
  "video_playlist",
  "video_local_m3u",
  "video_primary",
  "video_fallbacks",
  "audio_playlist",
  "audio_local_m3u",
  "audio_channel",
  "audio_fallbacks",
  "offset_seconds",
  "timeout_seconds",
  "retry_limit",
  "segment_time",
  "playlist_size",
  "channel_name",
  "recording_label",
  "ocr_provider",
  "ocrspace_api_key",
  "ocr_api_key",
  "ocr_custom_endpoint",
  "ocr_custom_model",
  "local_cache_enabled",
  "local_cache_seconds",
  "debug_cache_delay_seconds",
  "auto_align_interval",
  "auto_align_threshold",
  "auto_align_max_offset",
  "auto_align_debug_override",
  "schedule_enabled",
  "schedule_recording_enabled",
  "schedule_selected_event_ids",
  "schedule_pre_minutes",
  "schedule_duration_minutes",
  "schedule_post_minutes",
];
const stageLabels = {
  stopped: "已停止",
  starting: "启动中",
  running: "运行中",
  stopping: "停止中",
  error: "错误",
  waiting: "等待下一轮",
  probing: "截图识别中",
  verifying: "验证中",
  aligned: "已对齐",
  disabled: "已关闭",
  capture_failed: "截图失败",
};

function formatProbeTime(unixTs) {
  const ts = Number(unixTs || 0);
  if (!ts) return "";
  const date = new Date(ts * 1000);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleTimeString("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}

function humanizeAlignMessage(status, monitor) {
  const msg = String(status.auto_align_msg || monitor.message || "").trim();
  const state = String(status.auto_align_state || monitor.state || "").trim();
  const nextProbe = formatProbeTime(monitor.next_probe_at);
  if (!msg) return "-";
  if (msg.startsWith("source cache unavailable:")) {
    const detail = msg.replace("source cache unavailable:", "").trim();
    return `本地缓存不可用：${detail}`;
  }
  if (msg.startsWith("frame capture failed:")) {
    const detail = msg.replace("frame capture failed:", "").trim();
    return `本地抓帧失败：${detail}`;
  }
  if (msg.startsWith("capture skew too large:")) {
    return "视频/音频抓帧时间差过大，已丢弃本轮，不是 OCR 错误";
  }
  if (msg === "verify OCR failed" || msg.includes("OCR failed")) {
    return "OCR 识别失败：当前 provider 未识别到有效计时器，未继续 fallback";
  }
  if (msg.startsWith("OCR 未配置")) {
    return msg;
  }
  if (msg === "waiting for OCR" || msg === "waiting for next auto-align probe") {
    if (nextProbe) {
      return `等待下一次自动探测，预计 ${nextProbe}`;
    }
    return "等待下一次自动探测";
  }
  if (monitor.verify_message && msg !== monitor.verify_message) {
    return `${msg}；${monitor.verify_message}`;
  }
  if (state === "waiting" && nextProbe && !msg.startsWith("等待下一次自动探测")) {
    return `${msg}；下一次 ${nextProbe}`;
  }
  return msg;
}

let toastTimer;
let loadedOnce = false;
let autoLoadedChannels = false;
const selectedChannels = {
  video: null,
  audio: null,
};
const channelStore = {
  video: [],
  audio: [],
};
const chosenChannels = {
  video: [],
  audio: [],
};
let activeDrag = null;
let activeView = "overview";

const viewTitles = {
  overview: "总览",
  sources: "源配置",
  alignment: "自动对齐",
  tools: "工具与录制",
  logs: "日志",
};

function showToast(message) {
  toast.textContent = message;
  toast.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove("show"), 3600);
}

function setActiveView(nextView) {
  const target = String(nextView || "").trim();
  if (!target) return;
  activeView = target;
  navLinks.forEach((button) => {
    button.classList.toggle("active", button.dataset.viewTarget === target);
  });
  views.forEach((section) => {
    section.classList.toggle("active", section.dataset.view === target);
  });
  if (viewTitle) {
    viewTitle.textContent = viewTitles[target] || "直播同步管理";
  }
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok || data.error) {
    throw new Error(data.error || `请求失败：${response.status}`);
  }
  return data;
}

function profileFromForm() {
  syncFormFieldsFromChosen("video");
  syncFormFieldsFromChosen("audio");
  const profile = {};
  for (const name of profileFields) {
    const el = form.elements[name];
    if (name === "schedule_selected_event_ids") {
      profile[name] = selectedScheduleEventIdsFromDom();
      continue;
    }
    if (!el) continue;
    if (["video_fallbacks", "audio_fallbacks"].includes(name)) {
      profile[name] = el.value
        .split("\n")
        .map((item) => item.trim())
        .filter(Boolean);
    } else if (["offset_seconds", "segment_time", "auto_align_threshold", "auto_align_max_offset", "debug_cache_delay_seconds"].includes(name)) {
      profile[name] = Number(el.value || 0);
    } else if ([
      "timeout_seconds",
      "retry_limit",
      "playlist_size",
      "local_cache_seconds",
      "auto_align_interval",
      "schedule_pre_minutes",
      "schedule_duration_minutes",
      "schedule_post_minutes",
    ].includes(name)) {
      profile[name] = Number.parseInt(el.value || "0", 10);
    } else if (el.type === "checkbox") {
      profile[name] = el.checked;
    } else if (["video_playlist", "audio_playlist", "video_headers", "audio_headers"].includes(name)) {
      profile[name] = el.value
        .split("\n")
        .map((item) => item.trim())
        .filter(Boolean)
        .join("\n");
    } else {
      profile[name] = el.value.trim();
    }
  }
  return profile;
}

function fillForm(profile) {
  const mergedProfile = profile || {};
  for (const name of profileFields) {
    const el = form.elements[name];
    if (!el) continue;
    const value = mergedProfile[name];
    if (el.type === "checkbox") {
      el.checked = Boolean(value);
    } else {
      el.value = Array.isArray(value) ? value.join("\n") : value ?? "";
    }
  }
  hydrateChosenChannelsFromForm();
  renderSelectedChannels("video");
  renderSelectedChannels("audio");
}

function setBusy(isBusy) {
  document.querySelectorAll("button").forEach((button) => {
    button.disabled = isBusy;
  });
}

function hlsAge(mtime) {
  if (!mtime) return "缺失";
  const age = Math.max(0, Math.round(Date.now() / 1000 - mtime));
  return age < 2 ? "刚更新" : `${age} 秒前`;
}

function formatMatch(match) {
  if (!match) return "-";
  const start = match.window_start ? match.window_start.replace("T", " ") : "";
  const end = match.window_end ? match.window_end.replace("T", " ") : "";
  return `${match.short_name || match.name} ${start}${end ? ` - ${end}` : ""}`;
}

function zhStatus(value) {
  return stageLabels[value] || value || "-";
}

function updateRecordingStatus(recording) {
  const root = document.querySelector("#recordingStatus");
  if (!root) return;
  const active = recording?.running ? recording?.active : null;
  if (!active) {
    root.textContent = "未开始";
    return;
  }
  const state = zhStatus(active.status);
  const merge = active.merge_status ? ` / ${active.merge_status}` : "";
  const segs = active.segment_count != null ? ` / ${active.segment_count} 段` : "";
  root.textContent = `${active.label || active.session_id} · ${state}${merge}${segs}`;
}

function selectedScheduleEventIdsFromDom() {
  return Array.from(document.querySelectorAll('input[name="schedule_selected_event_ids"]:checked'))
    .map((el) => String(el.value || "").trim())
    .filter(Boolean);
}

function renderScheduleMatchPicker(schedule = {}) {
  const root = document.querySelector("#scheduleMatchPicker");
  if (!root) return;
  root.innerHTML = "";
  const matches = Array.isArray(schedule.upcoming_matches) ? schedule.upcoming_matches : [];
  if (!matches.length) {
    root.textContent = "今明两天暂无可选比赛。";
    return;
  }
  const selectedIds = new Set(Array.isArray(schedule.selected_event_ids) ? schedule.selected_event_ids : []);
  matches.forEach((match) => {
    const label = document.createElement("label");
    label.className = "field full checkbox-field";
    const input = document.createElement("input");
    input.type = "checkbox";
    input.name = "schedule_selected_event_ids";
    input.value = match.event_id || "";
    input.checked = selectedIds.size ? selectedIds.has(match.event_id) : Boolean(match.selected);
    const text = document.createElement("span");
    const start = match.window_start ? match.window_start.replace("T", " ") : "";
    text.textContent = `${match.short_name || match.name || "-"} ${start}`;
    label.append(input, text);
    root.append(label);
  });
}

function updateStatus(status) {
  if (!loadedOnce) {
    fillForm(status.profile || {});
    loadedOnce = true;
  }

  const running = Boolean(status.running);
  const runBadge = document.querySelector("#runBadge");
  runBadge.textContent = running ? "运行中" : "已停止";
  runBadge.classList.toggle("running", running);

  const hlsBadge = document.querySelector("#hlsBadge");
  hlsBadge.textContent = status.hls?.playlist_exists ? `HLS ${hlsAge(status.hls.playlist_mtime)}` : "HLS 缺失";
  hlsBadge.classList.toggle("running", Boolean(status.hls?.playlist_exists));

  document.querySelector("#streamSummary").textContent =
    status.last_error || `${status.profile?.channel_name || "直播"} -> ${status.hls_url || "/index.m3u8"}`;
  document.querySelector("#stage").textContent = zhStatus(status.stage);
  document.querySelector("#activeChannel").textContent = status.active_channel || "-";
  document.querySelector("#offsetValue").textContent = `${Number(status.offset_seconds || 0).toFixed(3)}s`;
  const aa = status.auto_align || {};
  const alignEl = document.querySelector("#alignStatus");
  if (!aa.enabled) {
    alignEl.textContent = "关闭";
    alignEl.style.color = "inherit";
  } else if (!aa.active_allowed) {
    alignEl.textContent = `暂停（非比赛时间）`;
    alignEl.style.color = "var(--muted)";
  } else if (aa.debug_override) {
    alignEl.textContent = `开启（调试覆盖，${aa.interval}s）`;
    alignEl.style.color = "var(--ok)";
  } else {
    alignEl.textContent = `开启（${aa.interval}s）`;
    alignEl.style.color = "var(--ok)";
  }
  const monitor = status.auto_align_monitor || {};
  document.querySelector("#alignState").textContent = zhStatus(status.auto_align_state || monitor.state);
  document.querySelector("#alignMonitor").textContent = monitor.state
    ? [
        `候选 视频:${monitor.candidate_video_clock || "-"} 音频:${monitor.candidate_audio_clock || "-"}${monitor.last_candidate_offset != null ? ` offset:${Number(monitor.last_candidate_offset).toFixed(3)}s` : ""}${monitor.candidate_video_seconds_back != null && monitor.candidate_audio_seconds_back != null ? ` seek:${Number(monitor.candidate_video_seconds_back).toFixed(3)}/${Number(monitor.candidate_audio_seconds_back).toFixed(3)}s` : ""}`,
        `验证 视频:${monitor.verify_video_clock || "-"} 音频:${monitor.verify_audio_clock || "-"}${monitor.verify_delta != null ? ` delta:${Number(monitor.verify_delta).toFixed(1)}s` : ""}${monitor.verify_video_seconds_back != null && monitor.verify_audio_seconds_back != null ? ` seek:${Number(monitor.verify_video_seconds_back).toFixed(3)}/${Number(monitor.verify_audio_seconds_back).toFixed(3)}s` : ""}`,
      ].join(" | ")
    : "-";
  document.querySelector("#lastAlignTime").textContent = status.last_alignment || "-";
  document.querySelector("#alignMsg").textContent = humanizeAlignMessage(status, monitor);
  document.querySelector("#lastSnapshotTime").textContent = status.last_snapshot_at || "-";
  renderOcrResults(status);
  const schedule = status.schedule || {};
  renderScheduleMatchPicker(schedule);
  const scheduleEl = document.querySelector("#scheduleStatus");
  scheduleEl.textContent = schedule.enabled ? (schedule.active ? "比赛窗口" : "等待比赛") : "关闭";
  scheduleEl.style.color = schedule.enabled && schedule.active ? "var(--ok)" : "inherit";
  document.querySelector("#scheduleActive").textContent = formatMatch(schedule.active_match);
  document.querySelector("#scheduleNext").textContent = formatMatch(schedule.next_match);
  document.querySelector("#scheduleRefresh").textContent = schedule.last_refresh || "-";
  document.querySelector("#scheduleMsg").textContent = schedule.message || "-";
  const scheduleStatusCard = document.querySelector("#scheduleStatusCard");
  const scheduleActiveCard = document.querySelector("#scheduleActiveCard");
  const scheduleNextCard = document.querySelector("#scheduleNextCard");
  if (scheduleStatusCard) scheduleStatusCard.textContent = schedule.enabled ? (schedule.active ? "比赛窗口" : "等待比赛") : "关闭";
  if (scheduleActiveCard) scheduleActiveCard.textContent = formatMatch(schedule.active_match);
  if (scheduleNextCard) scheduleNextCard.textContent = formatMatch(schedule.next_match);
  document.querySelector("#failures").textContent = status.failure_count ?? 0;
  document.querySelector("#segmentCount").textContent = status.hls?.segment_count ?? 0;
  document.querySelector("#playlistState").textContent = status.hls?.playlist_exists ? "就绪" : "缺失";
  document.querySelector("#latestSegment").textContent = status.hls?.latest_segment || "-";
  document.querySelector("#lastSegment").textContent = status.last_segment_at || "-";
  document.querySelector("#lastError").textContent = status.last_error || "";
  document.querySelector("#hlsUrl").value = `${window.location.origin}${status.hls_url || "/index.m3u8"}`;
  document.querySelector("#hlsLink").href = status.hls_url || "/index.m3u8";
  document.querySelector("#embyLink").href = status.emby_url || "/emby.m3u";
  updateRecordingStatus(status.recording || {});
}

async function refresh() {
  const [status, logs] = await Promise.all([
    api("/api/status"),
    api("/api/logs"),
  ]);
  updateStatus(status);
  document.querySelector("#logOutput").textContent = (logs.lines || []).join("\n") || "暂无日志。";
  loadSnapshots().catch(() => {});
  autoLoadChannelsOnce().catch(() => {});
  loadRecordings().catch(() => {});
}

async function runAction(label, fn) {
  setBusy(true);
  try {
    const result = await fn();
    await refresh();
    showToast(label);
    return result;
  } catch (error) {
    showToast(error.message);
  } finally {
    setBusy(false);
  }
}

function renderOcrResults(status) {
  const root = document.querySelector("#ocrResults");
  if (!root) return;
  root.innerHTML = "";
  const ocr = status.last_ocr_results || {};
  const monitor = status.auto_align_monitor || {};
  const routeText = (route) => {
    if (route === "remote_request_failed -> rapidocr_local") return "primary/backup request failed -> rapidocr_local";
    if (route === "ocrspace") return "primary/backup resolved at ocrspace";
    if (route === "custom") return "primary/backup resolved at custom";
    if (route === "rapidocr_local") return "rapidocr_local";
    return route || "";
  };
  const providerRouteLabel = (data) => {
    if (!data) return "";
    if (data.route) return `provider route: ${routeText(data.route)}`;
    if (data.provider === "ocrspace") return "provider route: primary/backup resolved at ocrspace";
    if (data.provider === "custom") return "provider route: primary/backup resolved at custom";
    if (data.provider === "rapidocr_local") return "provider route: rapidocr_local";
    return "";
  };
  const formatOcrMeta = (data, monitorClock) => {
    if (data && data.clock) {
      const parts = [];
      if (data.updated_at) parts.push(`更新于 ${data.updated_at}`);
      if (data.provider === "rapidocr_local" && data.note) {
        parts.push("primary/backup 请求失败，已回退本地 RapidOCR");
      } else if (data.provider === "rapidocr_local") {
        parts.push("本地 RapidOCR");
      } else if (data.provider === "ocrspace") {
        parts.push("OCR.space");
      } else if (data.provider === "custom") {
        parts.push("自定义 OCR");
      }
      return parts.join(" · ");
    }
    if (monitorClock) {
      return "监控中";
    }
    if (data && data.detail_error) {
      return "primary 未识别到有效计时器，已停止继续 fallback";
    }
    return "未识别到时间";
  };
  const groups = [
    { title: "缓存截图", videoKind: "cache_video", audioKind: "cache_audio" },
    { title: "验证截图", videoKind: "video", audioKind: "audio" },
  ];
  for (const group of groups) {
    const panel = document.createElement("div");
    panel.className = "ocr-panel";

    const title = document.createElement("h3");
    title.textContent = group.title;
    panel.append(title);

    for (const entry of [
      { label: "视频", kind: group.videoKind },
      { label: "音频", kind: group.audioKind },
    ]) {
      const row = document.createElement("div");
      row.className = "ocr-row";
      const labelEl = document.createElement("div");
      labelEl.className = "ocr-label";
      labelEl.textContent = entry.label;
      const clockEl = document.createElement("div");
      clockEl.className = "ocr-clock";
      const timeEl = document.createElement("div");
      timeEl.className = "ocr-time";
      const routeEl = document.createElement("div");
      routeEl.className = "ocr-route";
      const data = ocr[entry.kind];
      const monitorClock = entry.kind === "video" ? monitor.video_clock : entry.kind === "audio" ? monitor.audio_clock : "";
      if (data && data.clock) {
        clockEl.textContent = data.clock;
        timeEl.textContent = formatOcrMeta(data, monitorClock);
        routeEl.textContent = providerRouteLabel(data);
      } else if (monitorClock) {
        clockEl.textContent = monitorClock;
        timeEl.textContent = formatOcrMeta(data, monitorClock);
        routeEl.textContent = providerRouteLabel(data);
      } else {
        clockEl.textContent = "-";
        timeEl.textContent = formatOcrMeta(data, monitorClock);
        routeEl.textContent = providerRouteLabel(data);
      }
      row.append(labelEl, clockEl, timeEl, routeEl);
      panel.append(row);
    }

    root.append(panel);
  }
}

function renderSnapshots(items) {
  const root = document.querySelector("#snapshotGallery");
  if (!root) return;
  root.innerHTML = "";
  const groups = [
    { title: "缓存截图", videoKind: "cache_video", audioKind: "cache_audio" },
    { title: "验证截图", videoKind: "video", audioKind: "audio" },
  ];
  for (const group of groups) {
    const panel = document.createElement("div");
    panel.className = "snapshot-panel";

    const title = document.createElement("strong");
    title.className = "snapshot-panel-title";
    title.textContent = group.title;
    panel.append(title);

    for (const entry of [
      { label: "视频", kind: group.videoKind },
      { label: "音频", kind: group.audioKind },
    ]) {
      const item = (items || []).find((snapshot) => snapshot.kind === entry.kind) || {
        kind: entry.kind,
        url: "",
        name: `${entry.kind}_snapshot.jpg`,
        mtime: 0,
      };
      const slot = document.createElement(item.url ? "a" : "div");
      slot.className = "snapshot-slot";
      if (item.url) {
        slot.href = `${item.url}?t=${item.mtime || 0}`;
        slot.target = "_blank";
      }

      const label = document.createElement("span");
      label.className = "snapshot-slot-label";
      label.textContent = entry.label;
      slot.append(label);

      const img = document.createElement("img");
      img.alt = `${group.title}${entry.label}`;
      img.loading = "lazy";
      if (item.url) {
        img.src = `${item.url}?t=${item.mtime || 0}`;
      }
      slot.append(img);

      const meta = document.createElement("span");
      meta.textContent = item.url ? item.name : "暂无截图";
      slot.append(meta);

      panel.append(slot);
    }

    root.append(panel);
  }
}

function escHtml(str) {
  const div = document.createElement("div");
  div.textContent = str || "";
  return div.innerHTML;
}

function renderRecordings(items) {
  const root = document.querySelector("#recordingList");
  if (!root) return;
  root.innerHTML = "";
  if (!items.length) {
    root.textContent = "暂无录制。";
    return;
  }
  items.forEach((item) => {
    const row = document.createElement("div");
    row.className = "recording-row";
    const meta = document.createElement("div");
    meta.className = "recording-meta";
    const title = document.createElement("strong");
    title.textContent = item.label || item.session_id || "-";
    const sub = document.createElement("span");
    sub.textContent = `${item.status || "-"} / ${item.merge_status || "-"} / ${item.segment_count ?? 0} 段`;
    meta.append(title, sub);
    const actions = document.createElement("div");
    actions.className = "recording-actions";

    const playlist = document.createElement("a");
    playlist.className = "link-button";
    playlist.href = item.playlist_url || "#";
    playlist.target = "_blank";
    playlist.textContent = "播放列表";
    actions.append(playlist);

    if (item.merged_url) {
      const merged = document.createElement("a");
      merged.className = "link-button";
      merged.href = item.merged_url;
      merged.target = "_blank";
      merged.textContent = "单文件";
      actions.append(merged);
    }

    const exportBtn = document.createElement("button");
    exportBtn.type = "button";
    exportBtn.className = "secondary";
    exportBtn.textContent = "导出 MKV";
    exportBtn.addEventListener("click", async () => {
      try {
        await api("/api/recording/merge", {
          method: "POST",
          body: JSON.stringify({
            session_id: item.session_id,
            output_format: document.querySelector("#recordingExportFormat").value,
          }),
        });
        await loadRecordings();
        showToast("录制已合并");
      } catch (e) {
        showToast(e.message);
      }
    });
    actions.append(exportBtn);

    const deleteBtn = document.createElement("button");
    deleteBtn.type = "button";
    deleteBtn.className = "secondary";
    deleteBtn.textContent = "删除";
    if (["starting", "running", "stopping"].includes(String(item.status || ""))) {
      deleteBtn.disabled = true;
      deleteBtn.title = "运行中的录制不能删除";
    }
    deleteBtn.addEventListener("click", async () => {
      if (!window.confirm(`确认删除录制“${item.label || item.session_id || "未命名"}”吗？`)) return;
      try {
        await api("/api/recording/delete", {
          method: "POST",
          body: JSON.stringify({ session_id: item.session_id }),
        });
        await loadRecordings();
        showToast("录制已删除");
      } catch (e) {
        showToast(e.message);
      }
    });
    actions.append(deleteBtn);

    row.append(meta, actions);
    root.append(row);
  });
}

async function loadRecordings() {
  const data = await api("/api/recordings");
  renderRecordings(data.recordings || []);
}

async function loadSnapshots() {
  const data = await api("/api/snapshots");
  renderSnapshots(data.snapshots || []);
}

async function loadChannels(kind, { force = true, quiet = false, serverFilter = true } = {}) {
  const profile = profileFromForm();
  const url = kind === "audio" ? profile.audio_playlist : profile.video_playlist;
  const text = kind === "audio" ? profile.audio_local_m3u : profile.video_local_m3u;
  if (!url && !text) {
    if (!quiet) showToast(`${kind === "audio" ? "音频" : "视频"} M3U 为空`);
    return;
  }
  const query = serverFilter
    ? (kind === "audio"
        ? document.querySelector("#audioChannelFilter").value.trim()
        : document.querySelector("#videoChannelFilter").value.trim())
    : "";
  const data = await api("/api/playlists/preview", {
    method: "POST",
    body: JSON.stringify({
      url,
      text,
      label: kind === "audio" ? "本地音频 M3U" : "本地视频 M3U",
      query,
      force,
    }),
  });
  channelStore[kind] = data.channels || [];
  chosenChannels[kind] = chosenChannels[kind].map((item) => {
    const channel = findStoredChannel(kind, item.name);
    return {
      ...item,
      group: channelDescription(channel) || item.group,
    };
  });
  renderSelectedChannels(kind);
  if (data.errors?.length && !quiet) {
    showToast(`部分 M3U 加载失败：${data.errors.slice(0, 2).join("；")}`);
  }
  applyChannelFilter(kind);
}

async function autoLoadChannelsOnce() {
  if (autoLoadedChannels) return;
  autoLoadedChannels = true;
  await Promise.allSettled([
    loadChannels("video", { force: false, quiet: true, serverFilter: false }),
    loadChannels("audio", { force: false, quiet: true, serverFilter: false }),
  ]);
}

function channelMatches(channel, query) {
  if (!query) return true;
  const haystack = [
    channel.name,
    channel.tvg_name,
    channel.tvg_id,
    channel.group,
    channel.url,
  ].join(" ").toLowerCase();
  return haystack.includes(query.toLowerCase());
}

function displayChannelName(channel) {
  if (!channel) return "";
  if (typeof channel === "string") return channel.trim();
  return (channel.name || channel.tvg_name || channel.tvg_id || "").trim();
}

function channelDescription(channel) {
  if (!channel || typeof channel === "string") return "";
  return (channel.group || channel.url || "").trim();
}

function findStoredChannel(kind, name) {
  const target = (name || "").trim().toLowerCase();
  if (!target) return null;
  return channelStore[kind].find((channel) => displayChannelName(channel).toLowerCase() === target) || null;
}

function chosenNames(kind) {
  return chosenChannels[kind].map((item) => item.name);
}

function syncFormFieldsFromChosen(kind) {
  const primaryField = form.elements[kind === "audio" ? "audio_channel" : "video_primary"];
  const fallbackField = form.elements[kind === "audio" ? "audio_fallbacks" : "video_fallbacks"];
  const names = chosenNames(kind);
  primaryField.value = names[0] || "";
  fallbackField.value = names.slice(1).join("\n");
}

function hydrateChosenChannelsFromForm() {
  [
    {
      kind: "video",
      primary: form.elements.video_primary?.value || "",
      fallbacks: form.elements.video_fallbacks?.value || "",
    },
    {
      kind: "audio",
      primary: form.elements.audio_channel?.value || "",
      fallbacks: form.elements.audio_fallbacks?.value || "",
    },
  ].forEach(({ kind, primary, fallbacks }) => {
    const items = [primary, ...fallbacks.split("\n")]
      .map((item) => item.trim())
      .filter(Boolean)
      .filter((item, index, list) => list.indexOf(item) === index)
      .map((name) => {
        const channel = findStoredChannel(kind, name);
        return {
          name,
          group: channelDescription(channel),
        };
      });
    chosenChannels[kind] = items;
    syncFormFieldsFromChosen(kind);
  });
}

function addChosenChannel(kind, channel) {
  const name = displayChannelName(channel);
  if (!name) return;
  const existingIndex = chosenChannels[kind].findIndex((item) => item.name === name);
  const item = {
    name,
    group: channelDescription(channel),
  };
  if (existingIndex >= 0) {
    chosenChannels[kind][existingIndex] = item;
  } else {
    chosenChannels[kind].push(item);
  }
  syncFormFieldsFromChosen(kind);
  renderSelectedChannels(kind);
  refreshChannelSelection(kind);
}

function removeChosenChannel(kind, name) {
  chosenChannels[kind] = chosenChannels[kind].filter((item) => item.name !== name);
  syncFormFieldsFromChosen(kind);
  renderSelectedChannels(kind);
  refreshChannelSelection(kind);
}

function clearChosenChannels(kind) {
  chosenChannels[kind] = [];
  syncFormFieldsFromChosen(kind);
  renderSelectedChannels(kind);
  refreshChannelSelection(kind);
}

function moveChosenChannel(kind, fromIndex, toIndex) {
  const items = chosenChannels[kind];
  if (fromIndex === toIndex || fromIndex < 0 || toIndex < 0 || fromIndex >= items.length || toIndex >= items.length) {
    return;
  }
  const [item] = items.splice(fromIndex, 1);
  items.splice(toIndex, 0, item);
  syncFormFieldsFromChosen(kind);
  renderSelectedChannels(kind);
  refreshChannelSelection(kind);
}

function refreshChannelSelection(kind) {
  const root = document.querySelector(kind === "audio" ? "#audioChannels" : "#videoChannels");
  if (!root) return;
  const selected = new Set(chosenNames(kind));
  root.querySelectorAll('input[type="checkbox"][data-channel-name]').forEach((checkbox) => {
    const checked = selected.has(checkbox.dataset.channelName || "");
    checkbox.checked = checked;
    checkbox.closest(".row")?.classList.toggle("selected", checked);
  });
}

function renderSelectedChannels(kind) {
  const root = document.querySelector(kind === "audio" ? "#audioSelectedChannels" : "#videoSelectedChannels");
  if (!root) return;
  root.innerHTML = "";
  const items = chosenChannels[kind];
  if (!items.length) {
    const empty = document.createElement("div");
    empty.className = "selected-source-empty";
    empty.textContent = "尚未选择频道。";
    root.append(empty);
    return;
  }
  items.forEach((item, index) => {
    const row = document.createElement("div");
    row.className = "selected-source-item";
    row.draggable = true;
    row.dataset.index = String(index);
    row.dataset.kind = kind;
    row.addEventListener("dragstart", (event) => {
      activeDrag = { kind, index };
      row.classList.add("dragging");
      event.dataTransfer.effectAllowed = "move";
      event.dataTransfer.setData("text/plain", `${kind}:${index}`);
    });
    row.addEventListener("dragend", () => {
      activeDrag = null;
      root.querySelectorAll(".selected-source-item").forEach((node) => node.classList.remove("dragging", "drag-over"));
    });
    row.addEventListener("dragover", (event) => {
      if (!activeDrag || activeDrag.kind !== kind) return;
      event.preventDefault();
      event.dataTransfer.dropEffect = "move";
      row.classList.add("drag-over");
    });
    row.addEventListener("dragleave", () => {
      row.classList.remove("drag-over");
    });
    row.addEventListener("drop", (event) => {
      event.preventDefault();
      row.classList.remove("drag-over");
      if (!activeDrag || activeDrag.kind !== kind) return;
      moveChosenChannel(kind, activeDrag.index, index);
    });

    const badge = document.createElement("span");
    badge.className = `selected-source-badge${index === 0 ? " primary" : ""}`;
    badge.textContent = index === 0 ? "首选" : `Fallback ${index}`;

    const meta = document.createElement("div");
    meta.className = "selected-source-meta";
    const title = document.createElement("strong");
    title.textContent = item.name;
    const sub = document.createElement("span");
    sub.textContent = item.group || "拖动调整优先级";
    meta.append(title, sub);

    const actions = document.createElement("div");
    actions.className = "selected-source-actions";

    const up = document.createElement("button");
    up.type = "button";
    up.className = "secondary";
    up.textContent = "上移";
    up.disabled = index === 0;
    up.addEventListener("click", () => moveChosenChannel(kind, index, index - 1));

    const down = document.createElement("button");
    down.type = "button";
    down.className = "secondary";
    down.textContent = "下移";
    down.disabled = index === items.length - 1;
    down.addEventListener("click", () => moveChosenChannel(kind, index, index + 1));

    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "danger";
    remove.textContent = "移除";
    remove.addEventListener("click", () => removeChosenChannel(kind, item.name));

    actions.append(up, down, remove);
    row.append(badge, meta, actions);
    root.append(row);
  });
}

function applyChannelFilter(kind) {
  const query =
    kind === "audio"
      ? document.querySelector("#audioChannelFilter").value.trim()
      : document.querySelector("#videoChannelFilter").value.trim();
  renderChannels(kind, channelStore[kind].filter((channel) => channelMatches(channel, query)), query);
}

function renderChannels(kind, channels, query = "") {
  selectedChannels[kind] = null;
  const root = document.querySelector(kind === "audio" ? "#audioChannels" : "#videoChannels");
  root.innerHTML = "";
  if (!channels.length) {
    root.textContent = query ? "没有匹配的频道。" : "尚未加载频道。";
    return;
  }
  channels.forEach((channel, index) => {
    const row = document.createElement("div");
    row.className = "row";
    row.innerHTML = `<span></span><input type="checkbox" /><strong></strong><small></small>`;
    row.querySelector("span").textContent = String(index + 1);
    const checkbox = row.querySelector('input[type="checkbox"]');
    const channelName = displayChannelName(channel);
    checkbox.dataset.channelName = channelName;
    checkbox.checked = chosenNames(kind).includes(channelName);
    row.querySelector("strong").textContent = channel.name || channel.tvg_name || channel.tvg_id || "未命名";
    row.querySelector("small").textContent = channel.group || channel.url;
    row.addEventListener("click", (event) => {
      if (event.target === checkbox) return;
      checkbox.checked = !checkbox.checked;
      checkbox.dispatchEvent(new Event("change", { bubbles: true }));
    });
    checkbox.addEventListener("change", () => {
      selectedChannels[kind] = channel;
      row.classList.toggle("selected", checkbox.checked);
      if (checkbox.checked) {
        addChosenChannel(kind, channel);
      } else {
        removeChosenChannel(kind, channelName);
      }
    });
    row.classList.toggle("selected", checkbox.checked);
    root.append(row);
  });
}

document.querySelector("#saveConfig").addEventListener("click", () => {
  runAction("配置已保存", () =>
    api("/api/profile", { method: "POST", body: JSON.stringify(profileFromForm()) })
  );
});

document.querySelector("#startBtn").addEventListener("click", () => {
  runAction("直播已启动", () =>
    api("/api/start", { method: "POST", body: JSON.stringify(profileFromForm()) })
  );
});

document.querySelector("#restartBtn").addEventListener("click", () => {
  runAction("直播已彻底重启", () =>
    api("/api/restart", {
      method: "POST",
      body: JSON.stringify({ ...profileFromForm(), clean: true }),
    })
  );
});

document.querySelector("#stopBtn").addEventListener("click", () => {
  runAction("直播已停止", () => api("/api/stop", { method: "POST", body: "{}" }));
});

document.querySelector("#refreshBtn").addEventListener("click", () => {
  runAction("状态已刷新", refresh);
});

document.querySelector("#refreshLogsOnly").addEventListener("click", () => {
  runAction("日志已刷新", refresh);
});

document.querySelector("#refreshSchedule").addEventListener("click", () => {
  runAction("赛程已刷新", () => api("/api/schedule/refresh", { method: "POST", body: "{}" }));
});

document.querySelector("#startRecording").addEventListener("click", () => {
  runAction("录制已开始", () =>
    api("/api/recording/start", {
      method: "POST",
      body: JSON.stringify({
        label: form.elements.recording_label?.value || "",
      }),
    })
  );
});

document.querySelector("#stopRecording").addEventListener("click", () => {
  runAction("录制已停止", () => api("/api/recording/stop", { method: "POST", body: "{}" }));
});

document.querySelector("#refreshRecordings").addEventListener("click", () => {
  runAction("录制列表已刷新", loadRecordings);
});

document.querySelector("#testOcr").addEventListener("click", () => {
  runAction("OCR 测试完成", () =>
    api("/api/ocr/test", { method: "POST", body: JSON.stringify(profileFromForm()) }).then((result) => {
      if (!result.ok) {
        throw new Error(result.message || "OCR 测试失败");
      }
      return result;
    })
  );
});

document.querySelector("#copyHls").addEventListener("click", async () => {
  await navigator.clipboard.writeText(document.querySelector("#hlsUrl").value);
  showToast("HLS 地址已复制");
});

document.querySelector("#refreshVideoChannels").addEventListener("click", () => {
  runAction("视频频道已加载", () => loadChannels("video", { serverFilter: false }));
});

document.querySelector("#refreshAudioChannels").addEventListener("click", () => {
  runAction("音频频道已加载", () => loadChannels("audio", { serverFilter: false }));
});

document.querySelector("#videoChannelFilter").addEventListener("input", () => {
  applyChannelFilter("video");
});

document.querySelector("#audioChannelFilter").addEventListener("input", () => {
  applyChannelFilter("audio");
});

document.querySelector("#clearVideoSelected").addEventListener("click", () => {
  clearChosenChannels("video");
});

document.querySelector("#clearAudioSelected").addEventListener("click", () => {
  clearChosenChannels("audio");
});

function captureBothShots() {
  runAction("同步截图已生成", async () => {
    const result = await api("/api/snapshots/capture", { method: "POST", body: "{}" });
    await loadSnapshots();
    return result;
  });
}

document.querySelector("#shotBoth").addEventListener("click", captureBothShots);

document.querySelector("#clearHls").addEventListener("click", () => {
  if (!window.confirm("清理已经生成的 HLS 文件？")) return;
  runAction("HLS 文件已清理", () =>
    api("/api/clear", { method: "POST", body: JSON.stringify({ target: "hls" }) })
  );
});

document.querySelector("#clearState").addEventListener("click", () => {
  if (!window.confirm("清理运行状态文件？配置文件会保留。")) return;
  runAction("运行状态已清理", () =>
    api("/api/clear", { method: "POST", body: JSON.stringify({ target: "state" }) })
  );
});

navLinks.forEach((button) => {
  button.addEventListener("click", () => {
    setActiveView(button.dataset.viewTarget);
  });
});

setActiveView(activeView);

refresh().catch((error) => showToast(error.message));
setInterval(() => {
  refresh().catch(() => {});
}, 5000);
