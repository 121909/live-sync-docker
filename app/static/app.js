const form = document.querySelector("#configForm");
const toast = document.querySelector("#toast");
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
  "offset_seconds",
  "timeout_seconds",
  "retry_limit",
  "segment_time",
  "playlist_size",
  "channel_name",
  "local_cache_enabled",
  "local_cache_seconds",
  "auto_align_enabled",
  "auto_align_stop_after_aligned",
  "auto_align_interval",
  "auto_align_samples",
  "auto_align_threshold",
  "auto_align_max_offset",
  "video_roi",
  "audio_roi",
  "video_roi_presets",
  "audio_roi_presets",
  "schedule_enabled",
  "auto_align_outside_match",
  "schedule_pre_minutes",
  "schedule_duration_minutes",
  "schedule_post_minutes",
];
const hiddenDefaults = {
  auto_align_step: 1,
  auto_align_relocate_attempts: 3,
};
const stageLabels = {
  stopped: "已停止",
  starting: "启动中",
  running: "运行中",
  acquiring: "寻找计时器",
  locked: "已锁定",
  aligned: "已对齐",
  mismatch: "时间不一致",
  realigning: "正在重对齐",
  disabled: "已关闭",
  capture_failed: "截图失败",
};

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

function showToast(message) {
  toast.textContent = message;
  toast.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove("show"), 3600);
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
  const profile = {};
  for (const name of profileFields) {
    const el = form.elements[name];
    if (!el) continue;
    if (name === "video_fallbacks") {
      profile[name] = el.value
        .split("\n")
        .map((item) => item.trim())
        .filter(Boolean);
    } else if (["offset_seconds", "segment_time", "auto_align_threshold", "auto_align_max_offset"].includes(name)) {
      profile[name] = Number(el.value || 0);
    } else if ([
      "timeout_seconds",
      "retry_limit",
      "playlist_size",
      "local_cache_seconds",
      "auto_align_interval",
      "auto_align_samples",
      "schedule_pre_minutes",
      "schedule_duration_minutes",
      "schedule_post_minutes",
    ].includes(name)) {
      profile[name] = Number.parseInt(el.value || "0", 10);
    } else if (el.type === "checkbox") {
      profile[name] = el.checked;
    } else if (["video_playlist", "audio_playlist", "video_headers", "audio_headers", "video_roi_presets", "audio_roi_presets"].includes(name)) {
      profile[name] = el.value
        .split("\n")
        .map((item) => item.trim())
        .filter(Boolean)
        .join("\n");
    } else {
      profile[name] = el.value.trim();
    }
  }
  Object.assign(profile, hiddenDefaults, profile);
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
  } else {
    alignEl.textContent = `开启（${aa.interval}s）`;
    alignEl.style.color = "var(--ok)";
  }
  const monitor = status.auto_align_monitor || {};
  document.querySelector("#alignState").textContent = zhStatus(status.auto_align_state || monitor.state);
  document.querySelector("#alignMonitor").textContent = monitor.state
    ? `视频:${monitor.video_clock || "-"} 音频:${monitor.audio_clock || "-"} 不一致:${monitor.mismatch_count || 0}`
    : "-";
  document.querySelector("#lastAlignTime").textContent = status.last_alignment || "-";
  document.querySelector("#alignMsg").textContent = status.auto_align_msg || "-";
  document.querySelector("#lastSnapshotTime").textContent = status.last_snapshot_at || "-";
  const schedule = status.schedule || {};
  const scheduleEl = document.querySelector("#scheduleStatus");
  scheduleEl.textContent = schedule.enabled ? (schedule.active ? "比赛窗口" : "等待比赛") : "关闭";
  scheduleEl.style.color = schedule.enabled && schedule.active ? "var(--ok)" : "inherit";
  document.querySelector("#scheduleActive").textContent = formatMatch(schedule.active_match);
  document.querySelector("#scheduleNext").textContent = formatMatch(schedule.next_match);
  document.querySelector("#scheduleRefresh").textContent = schedule.last_refresh || "-";
  document.querySelector("#scheduleMsg").textContent = schedule.message || "-";
  document.querySelector("#failures").textContent = status.failure_count ?? 0;
  document.querySelector("#segmentCount").textContent = status.hls?.segment_count ?? 0;
  document.querySelector("#playlistState").textContent = status.hls?.playlist_exists ? "就绪" : "缺失";
  document.querySelector("#latestSegment").textContent = status.hls?.latest_segment || "-";
  document.querySelector("#lastSegment").textContent = status.last_segment_at || "-";
  document.querySelector("#lastError").textContent = status.last_error || "";
  document.querySelector("#hlsUrl").value = `${window.location.origin}${status.hls_url || "/index.m3u8"}`;
  document.querySelector("#hlsLink").href = status.hls_url || "/index.m3u8";
  document.querySelector("#embyLink").href = status.emby_url || "/emby.m3u";
}

async function refresh() {
  const [status, logs, snapshots] = await Promise.all([
    api("/api/status"),
    api("/api/logs"),
    api("/api/snapshots"),
  ]);
  updateStatus(status);
  document.querySelector("#logOutput").textContent = (logs.lines || []).join("\n") || "暂无日志。";
  renderSnapshots(snapshots.snapshots || []);
  autoLoadChannelsOnce().catch(() => {});
}

async function runAction(label, fn) {
  setBusy(true);
  try {
    await fn();
    await refresh();
    showToast(label);
  } catch (error) {
    showToast(error.message);
  } finally {
    setBusy(false);
  }
}

function renderSnapshots(snapshots) {
  const root = document.querySelector("#snapshots");
  root.innerHTML = "";
  const byKind = Object.fromEntries((snapshots || []).map((shot) => [shot.kind || "", shot]));
  for (const kind of ["video", "audio"]) {
    const shot = byKind[kind] || { kind, name: `${kind}_snapshot.jpg`, url: "", mtime: 0 };
    const item = document.createElement(shot.url ? "a" : "div");
    item.className = "snapshot-slot";
    if (shot.url) {
      item.href = shot.url;
      item.target = "_blank";
    }
    const image = document.createElement("img");
    image.alt = `${kind} snapshot`;
    image.loading = "lazy";
    if (shot.url) {
      image.src = `${shot.url}?t=${shot.mtime}`;
    }
    const caption = document.createElement("span");
    caption.textContent = shot.url ? `${kind === "video" ? "视频" : "音频"}截图` : `${kind === "video" ? "视频" : "音频"}截图待生成`;
    item.append(image);
    item.append(caption);
    root.append(item);
  }
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
    const row = document.createElement("button");
    row.type = "button";
    row.className = "row";
    row.innerHTML = `<span>${index + 1}</span><strong></strong><small></small>`;
    row.querySelector("strong").textContent = channel.name || channel.tvg_name || channel.tvg_id || "未命名";
    row.querySelector("small").textContent = channel.group || channel.url;
    row.addEventListener("click", () => {
      root.querySelectorAll(".row").forEach((item) => item.classList.remove("selected"));
      row.classList.add("selected");
      selectedChannels[kind] = channel;
    });
    root.append(row);
  });
}

function selectedName(kind) {
  const channel = selectedChannels[kind];
  if (!channel) {
    showToast(`请先选择${kind === "audio" ? "音频" : "视频"}频道`);
    return "";
  }
  return channel.name || channel.tvg_name || channel.tvg_id || "";
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
  runAction("直播已重启", () =>
    api("/api/restart", { method: "POST", body: JSON.stringify(profileFromForm()) })
  );
});

document.querySelector("#stopBtn").addEventListener("click", () => {
  runAction("直播已停止", () => api("/api/stop", { method: "POST", body: "{}" }));
});

document.querySelector("#refreshBtn").addEventListener("click", () => {
  runAction("状态已刷新", refresh);
});

document.querySelector("#refreshSchedule").addEventListener("click", () => {
  runAction("赛程已刷新", () => api("/api/schedule/refresh", { method: "POST", body: "{}" }));
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

document.querySelector("#usePrimary").addEventListener("click", () => {
  const name = selectedName("video");
  if (name) form.elements.video_primary.value = name;
});

document.querySelector("#addFallback").addEventListener("click", () => {
  const name = selectedName("video");
  if (!name) return;
  const el = form.elements.video_fallbacks;
  const current = el.value.trim();
  el.value = current ? `${current}\n${name}` : name;
});

document.querySelector("#useAudio").addEventListener("click", () => {
  const name = selectedName("audio");
  if (name) form.elements.audio_channel.value = name;
});

function captureShot(kind) {
  runAction(`${kind === "video" ? "视频" : "音频"}截图已生成`, () =>
    api("/api/snapshot", { method: "POST", body: JSON.stringify({ kind }) })
  );
}

function captureBothShots() {
  runAction("同步截图已生成", () =>
    api("/api/snapshots/capture", { method: "POST", body: "{}" })
  );
}

document.querySelector("#shotBoth").addEventListener("click", captureBothShots);
document.querySelector("#shotVideo").addEventListener("click", () => captureShot("video"));
document.querySelector("#shotAudio").addEventListener("click", () => captureShot("audio"));

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

refresh().catch((error) => showToast(error.message));
setInterval(() => {
  refresh().catch(() => {});
}, 5000);
