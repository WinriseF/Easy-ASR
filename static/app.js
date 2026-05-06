const state = {
  models: [],
  files: [],
  jobs: [],
  captureDevices: [],
  browserTabs: [],
  browserCandidates: [],
  sourceMode: "browser",
  activeJob: null,
  activeCapture: null,
  activeBrowserImport: null,
  eventSource: null,
  captureEventSource: null,
};

const TERMINAL_STATUSES = new Set(["completed", "failed", "cancelled"]);
const DEFAULT_BROWSER_HOME_URL = "https://www.bing.com";
const uiSelects = new Map();

const els = {
  serverStatus: document.getElementById("serverStatus"),
  existingFileSelect: document.getElementById("existingFileSelect"),
  audioFileInput: document.getElementById("audioFileInput"),
  dropZone: document.getElementById("dropZone"),
  dropTitle: document.getElementById("dropTitle"),
  dropSubtitle: document.getElementById("dropSubtitle"),
  startButton: document.getElementById("startButton"),
  reloadButton: document.getElementById("reloadButton"),
  refreshFilesButton: document.getElementById("refreshFilesButton"),
  jobCountText: document.getElementById("jobCountText"),
  fileCountText: document.getElementById("fileCountText"),
  jobQueue: document.getElementById("jobQueue"),
  activeJobTitle: document.getElementById("activeJobTitle"),
  activeJobStatus: document.getElementById("activeJobStatus"),
  progressFill: document.getElementById("progressFill"),
  progressText: document.getElementById("progressText"),
  durationText: document.getElementById("durationText"),
  engineText: document.getElementById("engineText"),
  downloadRow: document.getElementById("downloadRow"),
  segmentTable: document.getElementById("segmentTable"),
  copyTranscriptButton: document.getElementById("copyTranscriptButton"),
  sourcePanelTitle: document.getElementById("sourcePanelTitle"),
  sourceModeButtons: document.querySelectorAll(".source-mode-button"),
  sourcePanes: document.querySelectorAll(".source-pane"),
  captureDeviceSelect: document.getElementById("captureDeviceSelect"),
  startCaptureButton: document.getElementById("startCaptureButton"),
  stopCaptureButton: document.getElementById("stopCaptureButton"),
  captureStatusText: document.getElementById("captureStatusText"),
  captureDurationText: document.getElementById("captureDurationText"),
  captureLevelFill: document.getElementById("captureLevelFill"),
  browserEndpointInput: document.getElementById("browserEndpointInput"),
  browserHomeSelect: document.getElementById("browserHomeSelect"),
  browserTabSelect: document.getElementById("browserTabSelect"),
  launchBrowserButton: document.getElementById("launchBrowserButton"),
  refreshBrowserTabsButton: document.getElementById("refreshBrowserTabsButton"),
  probeBrowserButton: document.getElementById("probeBrowserButton"),
  browserReloadInput: document.getElementById("browserReloadInput"),
  browserListenSecondsInput: document.getElementById("browserListenSecondsInput"),
  browserCandidateSelect: document.getElementById("browserCandidateSelect"),
  transcribeBrowserButton: document.getElementById("transcribeBrowserButton"),
  browserStatusText: document.getElementById("browserStatusText"),
  engineSelect: document.getElementById("engineSelect"),
  modelNameInput: document.getElementById("modelNameInput"),
  languageSelect: document.getElementById("languageSelect"),
  modelNote: document.getElementById("modelNote"),
  cpuThreadsInput: document.getElementById("cpuThreadsInput"),
  chunkSecondsInput: document.getElementById("chunkSecondsInput"),
  batchSizeInput: document.getElementById("batchSizeInput"),
  mergeLengthInput: document.getElementById("mergeLengthInput"),
  computeTypeSelect: document.getElementById("computeTypeSelect"),
  whisperPresetSelect: document.getElementById("whisperPresetSelect"),
  applyTerminologyInput: document.getElementById("applyTerminologyInput"),
  terminologyEditor: document.getElementById("terminologyEditor"),
  saveTerminologyButton: document.getElementById("saveTerminologyButton"),
  logOutput: document.getElementById("logOutput"),
};

async function api(path, options = {}) {
  const response = await fetch(path, options);
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || response.statusText);
  }
  return response.json();
}

function log(message) {
  els.logOutput.textContent = `${new Date().toLocaleTimeString()} ${message}`;
}

function initUiSelects() {
  for (const select of document.querySelectorAll("select")) {
    createUiSelect(select);
  }
  document.addEventListener("click", (event) => {
    if (!event.target.closest(".ui-select")) {
      closeUiSelects();
    }
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      closeUiSelects();
    }
  });
}

function createUiSelect(select) {
  if (uiSelects.has(select)) {
    return;
  }
  if (!select.id) {
    select.id = `ui-native-select-${uiSelects.size + 1}`;
  }

  const wrapper = document.createElement("div");
  wrapper.className = "ui-select";
  wrapper.dataset.selectId = select.id;

  const trigger = document.createElement("button");
  trigger.className = "ui-select-trigger";
  trigger.type = "button";
  trigger.setAttribute("aria-haspopup", "listbox");
  trigger.setAttribute("aria-expanded", "false");

  const value = document.createElement("span");
  value.className = "ui-select-value";
  const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  icon.setAttribute("viewBox", "0 0 24 24");
  icon.setAttribute("aria-hidden", "true");
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", "m6 9 6 6 6-6");
  icon.appendChild(path);

  const menu = document.createElement("div");
  menu.className = "ui-select-menu";
  menu.setAttribute("role", "listbox");
  menu.hidden = true;

  trigger.append(value, icon);
  wrapper.append(trigger, menu);
  select.classList.add("native-select-hidden");
  select.tabIndex = -1;
  select.setAttribute("aria-hidden", "true");
  select.insertAdjacentElement("afterend", wrapper);

  trigger.addEventListener("click", (event) => {
    event.preventDefault();
    if (select.disabled) {
      return;
    }
    const willOpen = !wrapper.classList.contains("is-open");
    closeUiSelects(wrapper);
    setUiSelectOpen(select, willOpen);
  });
  select.addEventListener("change", () => syncUiSelect(select));

  uiSelects.set(select, { wrapper, trigger, value, menu });
  syncUiSelect(select);
}

function setUiSelectOpen(select, open) {
  const control = uiSelects.get(select);
  if (!control) {
    return;
  }
  control.wrapper.classList.toggle("is-open", open);
  control.trigger.setAttribute("aria-expanded", open ? "true" : "false");
  control.menu.hidden = !open;
}

function closeUiSelects(exceptWrapper = null) {
  for (const [select, control] of uiSelects) {
    if (control.wrapper !== exceptWrapper) {
      setUiSelectOpen(select, false);
    }
  }
}

function syncAllUiSelects() {
  for (const select of uiSelects.keys()) {
    syncUiSelect(select);
  }
}

function syncUiSelect(select) {
  const control = uiSelects.get(select);
  if (!control) {
    return;
  }
  const selected = select.selectedOptions[0] || select.options[0] || null;
  const label = selected?.textContent?.trim() || "请选择";
  control.value.textContent = label;
  control.value.classList.toggle("is-placeholder", !selected || !selected.value);
  control.trigger.disabled = select.disabled;
  control.trigger.title = label;
  control.trigger.setAttribute("aria-label", select.getAttribute("aria-label") || label);
  control.wrapper.classList.toggle("is-disabled", select.disabled);
  control.menu.innerHTML = "";

  for (const option of select.options) {
    const item = document.createElement("button");
    item.className = "ui-select-option";
    item.type = "button";
    item.disabled = option.disabled;
    item.dataset.value = option.value;
    item.setAttribute("role", "option");
    item.setAttribute("aria-selected", option.selected ? "true" : "false");
    item.classList.toggle("is-selected", option.selected);
    item.textContent = option.textContent;
    item.addEventListener("click", () => {
      if (option.disabled) {
        return;
      }
      select.value = option.value;
      select.dispatchEvent(new Event("change", { bubbles: true }));
      closeUiSelects();
    });
    control.menu.appendChild(item);
  }
}

async function loadAll() {
  const [health, models, files, terms, jobs, captureDevices] = await Promise.all([
    api("/api/health"),
    api("/api/models"),
    api("/api/files"),
    api("/api/terminology"),
    api("/api/jobs"),
    api("/api/capture/devices").catch((error) => ({
      available: false,
      devices: [],
      install_hint: cleanError(error),
    })),
  ]);
  els.serverStatus.textContent = health.ok ? "本地服务运行中" : "服务异常";
  state.models = models.models;
  state.files = files.files;
  state.jobs = jobs.jobs;
  state.captureDevices = captureDevices.devices || [];
  renderCounts();
  renderModels();
  renderFiles();
  renderSourceMode();
  renderCaptureDevices(captureDevices);
  renderBrowserTabs({ available: true, tabs: state.browserTabs });
  renderBrowserCandidates(state.browserCandidates);
  renderTerminology(terms.terms);
  renderQueue();
  syncAllUiSelects();
  if (!state.activeJob && state.jobs.length) {
    setActiveJob(state.jobs[0].id);
  }
  log("已刷新工作台");
}

function renderModels() {
  els.engineSelect.innerHTML = "";
  for (const model of state.models) {
    const option = document.createElement("option");
    option.value = model.id;
    option.textContent = `${model.label}${model.available ? "" : " (未安装)"}`;
    option.disabled = !model.available;
    option.dataset.description = model.description;
    option.dataset.defaultModel = model.default_model || "";
    option.dataset.installHint = model.install_hint || "";
    els.engineSelect.appendChild(option);
  }
  const firstAvailable = state.models.find((item) => item.available);
  if (firstAvailable) {
    els.engineSelect.value = firstAvailable.id;
  }
  updateModelNote();
  syncUiSelect(els.engineSelect);
}

function updateModelNote() {
  const model = state.models.find((item) => item.id === els.engineSelect.value);
  if (!model) {
    els.modelNote.textContent = "没有可用模型。";
    return;
  }
  els.modelNameInput.placeholder = model.default_model || "默认模型";
  els.modelNote.textContent = model.available ? model.description : model.install_hint;
}

function renderFiles() {
  const current = els.existingFileSelect.value;
  els.existingFileSelect.innerHTML = '<option value="">选择 input 目录文件</option>';
  for (const file of state.files) {
    const option = document.createElement("option");
    option.value = file.name;
    option.textContent = `${file.name} ${file.duration_seconds ? formatTime(file.duration_seconds) : ""}`;
    els.existingFileSelect.appendChild(option);
  }
  els.existingFileSelect.value = current;
  syncUiSelect(els.existingFileSelect);
}

function renderCaptureDevices(payload) {
  const current = els.captureDeviceSelect.value;
  els.captureDeviceSelect.innerHTML = "";
  if (!payload.available) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = payload.install_hint || "系统音频采集未安装";
    els.captureDeviceSelect.appendChild(option);
    els.startCaptureButton.disabled = true;
    syncUiSelect(els.captureDeviceSelect);
    return;
  }
  for (const device of payload.devices || []) {
    const option = document.createElement("option");
    option.value = String(device.index);
    option.textContent = `${device.name}${device.is_default ? " · 默认" : ""}`;
    els.captureDeviceSelect.appendChild(option);
  }
  if (!els.captureDeviceSelect.options.length) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "未找到播放输出设备";
    els.captureDeviceSelect.appendChild(option);
  }
  els.captureDeviceSelect.value = current || (payload.devices?.find((item) => item.is_default)?.index ?? "");
  els.startCaptureButton.disabled =
    Boolean(state.activeCapture && state.activeCapture.status === "recording") || !state.captureDevices.length;
  syncUiSelect(els.captureDeviceSelect);
}

function renderBrowserTabs(payload) {
  const current = els.browserTabSelect.value;
  els.browserTabSelect.innerHTML = "";
  const tabs = payload.tabs || [];
  state.browserTabs = tabs;
  if (!payload.available) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = payload.install_hint || "调试浏览器不可用";
    els.browserTabSelect.appendChild(option);
    els.probeBrowserButton.disabled = true;
    els.browserStatusText.textContent = "未连接";
    syncUiSelect(els.browserTabSelect);
    return;
  }
  if (!tabs.length) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "点击标签页连接调试浏览器";
    els.browserTabSelect.appendChild(option);
    els.probeBrowserButton.disabled = true;
    els.browserStatusText.textContent = "未发现标签页";
    syncUiSelect(els.browserTabSelect);
    return;
  }
  for (const tab of tabs) {
    const option = document.createElement("option");
    option.value = tab.id;
    option.textContent = tab.title || tab.url || tab.id;
    els.browserTabSelect.appendChild(option);
  }
  els.browserTabSelect.value = tabs.some((tab) => tab.id === current) ? current : tabs[0].id;
  els.probeBrowserButton.disabled = false;
  els.browserStatusText.textContent = `${tabs.length} 个标签页`;
  syncUiSelect(els.browserTabSelect);
}

function renderBrowserCandidates(candidates = [], notes = []) {
  els.browserCandidateSelect.innerHTML = "";
  state.browserCandidates = candidates;
  if (!candidates.length) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "先探测媒体源";
    els.browserCandidateSelect.appendChild(option);
    els.transcribeBrowserButton.disabled = true;
    if (notes.length) {
      els.browserStatusText.textContent = notes[0];
    }
    syncUiSelect(els.browserCandidateSelect);
    return;
  }
  for (const candidate of candidates) {
    const option = document.createElement("option");
    option.value = candidate.url;
    option.disabled = !candidate.supported;
    const duration = formatCandidateDuration(candidate.duration_seconds);
    const stateText = candidate.supported ? (candidate.recommended ? "推荐" : "可用") : "失败";
    option.textContent = `${duration} · ${stateText} · ${candidate.source} · ${candidate.title || candidate.url}`;
    option.dataset.reason = candidate.reason || "";
    els.browserCandidateSelect.appendChild(option);
  }
  const firstSupported = candidates.find((candidate) => candidate.recommended) || candidates.find((candidate) => candidate.supported);
  els.browserCandidateSelect.value = firstSupported ? firstSupported.url : "";
  els.transcribeBrowserButton.disabled = !firstSupported;
  const usableCount = candidates.filter((candidate) => candidate.supported).length;
  els.browserStatusText.textContent = firstSupported
    ? `可用 ${usableCount} 个，已选择 ${formatCandidateDuration(firstSupported.duration_seconds)}`
    : (notes[0] || "没有可直接转写的媒体源");
  syncUiSelect(els.browserCandidateSelect);
}

function formatCandidateDuration(value) {
  const seconds = Number(value) || 0;
  return seconds > 0 ? formatTime(seconds) : "0s";
}

function updateBrowserCandidateState() {
  const candidate = state.browserCandidates.find((item) => item.url === els.browserCandidateSelect.value);
  els.transcribeBrowserButton.disabled = !candidate || !candidate.supported;
  if (candidate?.reason) {
    els.browserStatusText.textContent = candidate.reason;
  }
}

function renderTerminology(terms) {
  els.terminologyEditor.value = JSON.stringify(terms, null, 2);
}

function renderQueue() {
  els.jobQueue.innerHTML = "";
  if (!state.jobs.length) {
    const empty = document.createElement("div");
    empty.className = "queue-item";
    empty.innerHTML = "<strong>暂无任务</strong><span>提交音频后会显示进度</span>";
    els.jobQueue.appendChild(empty);
    return;
  }
  for (const job of state.jobs) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "queue-item";
    button.innerHTML = `<strong>${escapeHtml(job.source_name)}</strong><span>${escapeHtml(jobQueueMeta(job))}</span>`;
    button.addEventListener("click", () => setActiveJob(job.id));
    els.jobQueue.appendChild(button);
  }
}

function jobQueueMeta(job) {
  const parts = [job.status, `${Math.round(job.progress * 100)}%`];
  if (job.duration_seconds) {
    parts.push(formatTime(job.duration_seconds));
  }
  return parts.join(" · ");
}

async function refreshJobs({ detail = false } = {}) {
  const payload = await api("/api/jobs");
  state.jobs = payload.jobs;
  renderCounts();
  renderQueue();
  if (state.activeJob) {
    const latest = state.jobs.find((job) => job.id === state.activeJob.id);
    if (latest) {
      const statusChanged = latest.status !== state.activeJob.status;
      const terminal = TERMINAL_STATUSES.has(latest.status);
      if (detail || (terminal && statusChanged)) {
        await refreshActiveJobDetail(latest.id);
      } else {
        state.activeJob = { ...state.activeJob, ...latest };
        renderActiveJob(state.activeJob, { includeSegments: false });
      }
    }
  }
}

function renderCounts() {
  els.jobCountText.textContent = String(state.jobs.length);
  els.fileCountText.textContent = String(state.files.length);
}

function setSourceMode(mode) {
  state.sourceMode = mode === "capture" ? "capture" : "browser";
  renderSourceMode();
}

function renderSourceMode() {
  for (const button of els.sourceModeButtons) {
    const active = button.dataset.sourceMode === state.sourceMode;
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-selected", active ? "true" : "false");
  }
  for (const pane of els.sourcePanes) {
    pane.hidden = pane.dataset.sourcePane !== state.sourceMode;
  }
  els.sourcePanelTitle.textContent = state.sourceMode === "browser" ? "调试浏览器" : "系统播放";
}

async function setActiveJob(jobId) {
  const job = await api(`/api/jobs/${jobId}`);
  state.activeJob = job;
  renderActiveJob(job);
  if (TERMINAL_STATUSES.has(job.status)) {
    closeJobEvents();
  } else {
    watchJob(job.id);
  }
}

async function refreshActiveJobDetail(jobId) {
  const job = await api(`/api/jobs/${jobId}`);
  state.activeJob = job;
  renderActiveJob(job);
}

function renderActiveJob(job, { includeSegments = true } = {}) {
  els.activeJobTitle.textContent = job.source_name || "等待输入";
  els.activeJobStatus.textContent = job.status || "idle";
  els.activeJobStatus.className = `job-state ${job.status || ""}`;
  const pct = Math.round((job.progress || 0) * 100);
  els.progressFill.style.width = `${pct}%`;
  els.progressText.textContent = `${pct}%`;
  els.durationText.textContent = job.duration_seconds ? formatTime(job.duration_seconds) : "--:--";
  els.engineText.textContent = job.options?.engine_id || job.engine_id || "未选择模型";
  if (includeSegments) {
    renderDownloads(job);
    renderSegments(job.segments || []);
  }
}

function renderCapture(capture) {
  state.activeCapture = capture;
  const status = capture?.status || "idle";
  const recording = status === "recording" || status === "starting" || status === "stopping";
  els.captureStatusText.textContent = status === "idle" ? "未开始" : status;
  els.captureDurationText.textContent = formatTime(capture?.duration_seconds || 0);
  els.captureLevelFill.style.width = `${Math.round((capture?.level || 0) * 100)}%`;
  els.startCaptureButton.disabled = recording || !state.captureDevices.length;
  els.stopCaptureButton.disabled = !recording;
  if (capture && (recording || status === "transcribing" || status === "completed" || capture.live_segments?.length)) {
    renderLiveCaptureTranscript(capture);
  }
}

function renderLiveCaptureTranscript(capture) {
  const terminal = capture.status === "completed" || capture.status === "failed";
  const liveJob = {
    id: `capture:${capture.id}`,
    source_name: "系统播放实时转写",
    status: capture.status,
    progress: terminal ? 1 : 0,
    duration_seconds: capture.duration_seconds,
    engine_id: "live-capture",
    outputs: {},
    segments: capture.live_segments || [],
  };
  state.activeJob = liveJob;
  renderActiveJob(liveJob);
}

function renderDownloads(job) {
  els.downloadRow.innerHTML = "";
  const outputs = job.outputs || {};
  for (const formatName of Object.keys(outputs)) {
    const link = document.createElement("a");
    link.href = `/api/jobs/${job.id}/download/${formatName}`;
    link.textContent = `下载 ${formatName.toUpperCase()}`;
    els.downloadRow.appendChild(link);
  }
}

function renderSegments(segments) {
  els.segmentTable.innerHTML = "";
  if (!segments.length) {
    els.segmentTable.innerHTML = '<tr><td colspan="4" class="empty-cell">暂无转写片段</td></tr>';
    return;
  }
  for (const segment of segments) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${segment.index}</td>
      <td>${segment.start == null ? "--:--" : formatTime(segment.start)}</td>
      <td>${segment.end == null ? "--:--" : formatTime(segment.end)}</td>
      <td>${escapeHtml(segment.text || "")}</td>
    `;
    els.segmentTable.appendChild(tr);
  }
}

function watchJob(jobId) {
  closeJobEvents();
  state.eventSource = new EventSource(`/api/jobs/${jobId}/events`);
  state.eventSource.onmessage = async (event) => {
    const payload = JSON.parse(event.data);
    log(payload.message);
    const terminal = TERMINAL_STATUSES.has(payload.type);
    await refreshJobs({ detail: terminal });
    if (terminal) {
      closeJobEvents();
    }
  };
  state.eventSource.onerror = () => {
    closeJobEvents();
  };
}

function closeJobEvents() {
  if (state.eventSource) {
    state.eventSource.close();
    state.eventSource = null;
  }
}

async function submitJob() {
  els.startButton.disabled = true;
  try {
    const form = new FormData();
    const file = els.audioFileInput.files[0];
    if (file) {
      form.append("file", file);
    } else {
      form.append("existing_file", els.existingFileSelect.value);
    }
    appendTranscriptionOptions(form);
    const job = await api("/api/jobs", { method: "POST", body: form });
    await refreshJobs();
    await setActiveJob(job.id);
    log("任务已提交");
  } catch (error) {
    log(`提交失败: ${cleanError(error)}`);
  } finally {
    els.startButton.disabled = false;
  }
}

async function startCapture() {
  els.startCaptureButton.disabled = true;
  try {
    const form = new FormData();
    form.append("device_index", els.captureDeviceSelect.value);
    appendTranscriptionOptions(form);
    const capture = await api("/api/capture/start", { method: "POST", body: form });
    renderCapture(capture);
    watchCapture(capture.id);
    log("系统音频采集已开始");
  } catch (error) {
    log(`系统音频采集失败: ${cleanError(error)}`);
    els.startCaptureButton.disabled = !state.captureDevices.length;
  }
}

async function stopCapture() {
  const captureId = state.activeCapture?.id;
  if (!captureId) {
    return;
  }
  els.stopCaptureButton.disabled = true;
  try {
    const capture = await api(`/api/capture/${captureId}/stop`, { method: "POST" });
    renderCapture(capture);
    closeCaptureEvents();
    log("系统音频采集已停止，剩余切片会继续转写");
  } catch (error) {
    log(`停止采集失败: ${cleanError(error)}`);
    els.stopCaptureButton.disabled = false;
  }
}

async function refreshCapture(captureId) {
  if (!captureId) {
    return;
  }
  const capture = await api(`/api/capture/${captureId}`);
  renderCapture(capture);
}

function watchCapture(captureId) {
  closeCaptureEvents();
  state.captureEventSource = new EventSource(`/api/capture/${captureId}/events`);
  state.captureEventSource.onmessage = async (event) => {
    const payload = JSON.parse(event.data);
    log(payload.message);
    await refreshCapture(captureId);
    if (TERMINAL_STATUSES.has(payload.type) || payload.type === "completed" || payload.type === "failed") {
      closeCaptureEvents();
    }
  };
  state.captureEventSource.onerror = () => {
    closeCaptureEvents();
  };
}

function closeCaptureEvents() {
  if (state.captureEventSource) {
    state.captureEventSource.close();
    state.captureEventSource = null;
  }
}

async function launchDebugBrowser() {
  els.launchBrowserButton.disabled = true;
  els.browserStatusText.textContent = "启动中";
  try {
    const form = new FormData();
    form.append("endpoint", els.browserEndpointInput.value.trim());
    form.append("start_url", els.browserHomeSelect?.value || DEFAULT_BROWSER_HOME_URL);
    const payload = await api("/api/browser/launch", { method: "POST", body: form });
    if (payload.endpoint) {
      els.browserEndpointInput.value = payload.endpoint;
    }
    renderBrowserTabs({ available: true, tabs: payload.tabs || [] });
    log(payload.already_running ? "调试浏览器已在运行" : "调试浏览器已启动");
  } catch (error) {
    renderBrowserTabs({ available: false, install_hint: cleanError(error), tabs: [] });
    log(`调试浏览器启动失败: ${cleanError(error)}`);
  } finally {
    els.launchBrowserButton.disabled = false;
  }
}

async function loadBrowserTabs() {
  els.refreshBrowserTabsButton.disabled = true;
  els.browserStatusText.textContent = "连接中";
  try {
    const endpoint = encodeURIComponent(els.browserEndpointInput.value.trim());
    const payload = await api(`/api/browser/tabs?endpoint=${endpoint}`);
    renderBrowserTabs(payload);
    log(payload.available ? `发现 ${payload.tabs.length} 个调试浏览器标签页` : `调试浏览器不可用: ${payload.install_hint}`);
  } catch (error) {
    renderBrowserTabs({ available: false, install_hint: cleanError(error), tabs: [] });
    log(`调试浏览器连接失败: ${cleanError(error)}`);
  } finally {
    els.refreshBrowserTabsButton.disabled = false;
  }
}

async function probeBrowserMedia() {
  const tabId = els.browserTabSelect.value;
  if (!tabId) {
    log("请先选择调试浏览器标签页");
    return;
  }
  els.probeBrowserButton.disabled = true;
  els.transcribeBrowserButton.disabled = true;
  els.browserStatusText.textContent = "探测并验证中";
  try {
    const form = new FormData();
    form.append("endpoint", els.browserEndpointInput.value.trim());
    form.append("tab_id", tabId);
    form.append("listen_seconds", els.browserListenSecondsInput.value);
    form.append("reload_page", els.browserReloadInput.checked ? "true" : "false");
    const payload = await api("/api/browser/probe", { method: "POST", body: form });
    renderBrowserCandidates(payload.candidates || [], payload.notes || []);
    const note = (payload.notes || [])[0];
    log(note || (payload.recommended_url ? "已自动选择推荐媒体源" : `发现 ${payload.candidates.length} 个浏览器媒体候选`));
  } catch (error) {
    renderBrowserCandidates([], [cleanError(error)]);
    log(`浏览器媒体探测失败: ${cleanError(error)}`);
  } finally {
    els.probeBrowserButton.disabled = !els.browserTabSelect.value;
  }
}

async function transcribeBrowserMedia() {
  const sourceUrl = els.browserCandidateSelect.value;
  if (!sourceUrl) {
    log("没有可提交的浏览器媒体源");
    return;
  }
  els.transcribeBrowserButton.disabled = true;
  els.browserStatusText.textContent = "提取中";
  try {
    const form = new FormData();
    form.append("endpoint", els.browserEndpointInput.value.trim());
    form.append("tab_id", els.browserTabSelect.value);
    form.append("source_url", sourceUrl);
    appendTranscriptionOptions(form);
    const record = await api("/api/browser/transcribe", { method: "POST", body: form });
    state.activeBrowserImport = record;
    log("浏览器原始媒体正在提取");
  } catch (error) {
    els.browserStatusText.textContent = "提交失败";
    els.transcribeBrowserButton.disabled = false;
    log(`浏览器原源转写失败: ${cleanError(error)}`);
  }
}

async function refreshBrowserImport(importId) {
  const record = await api(`/api/browser/imports/${importId}`);
  state.activeBrowserImport = record;
  els.browserStatusText.textContent = browserImportStatusText(record);
  if (record.status === "submitted" && record.job_id) {
    state.activeBrowserImport = null;
    log(`浏览器媒体已提交转写队列${record.duration_seconds ? `，音频时长 ${formatTime(record.duration_seconds)}` : ""}`);
    await refreshJobs();
    await setActiveJob(record.job_id);
  } else if (record.status === "failed") {
    state.activeBrowserImport = null;
    els.transcribeBrowserButton.disabled = !els.browserCandidateSelect.value;
    log(`浏览器媒体提取失败: ${record.error}`);
  }
}

function browserImportStatusText(record) {
  const duration = record?.duration_seconds ? formatTime(record.duration_seconds) : "";
  if (record.status === "extracting") {
    return duration ? `提取中 ${duration}` : "提取中";
  }
  if (record.status === "submitted") {
    return duration ? `已提交 ${duration}` : "已提交";
  }
  if (record.status === "failed") {
    return "失败";
  }
  return record.status || "未连接";
}

function appendTranscriptionOptions(form) {
  form.append("engine_id", els.engineSelect.value);
  form.append("model_name", els.modelNameInput.value.trim());
  form.append("language", els.languageSelect.value);
  form.append("chunk_seconds", els.chunkSecondsInput.value);
  form.append("batch_size_s", els.batchSizeInput.value);
  form.append("merge_length_s", els.mergeLengthInput.value);
  form.append("cpu_threads", els.cpuThreadsInput.value);
  form.append("compute_type", els.computeTypeSelect.value);
  form.append("whisper_preset", els.whisperPresetSelect.value);
  form.append("apply_terminology", els.applyTerminologyInput.checked ? "true" : "false");
  form.append("output_formats", selectedFormats().join(","));
}

function selectedFormats() {
  return [...document.querySelectorAll(".formatInput:checked")].map((item) => item.value);
}

async function saveTerminology() {
  try {
    const parsed = JSON.parse(els.terminologyEditor.value);
    const terms = Array.isArray(parsed) ? parsed : parsed.terms;
    const payload = await api("/api/terminology", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ terms }),
    });
    renderTerminology(payload.terms);
    log("术语库已保存");
  } catch (error) {
    log(`术语库保存失败: ${cleanError(error)}`);
  }
}

async function copyTranscript() {
  const segments = state.activeJob?.segments || [];
  const text = segments.map((segment) => segment.text).join("\n\n").trim();
  if (!text) {
    log("没有可复制的文本");
    return;
  }
  await navigator.clipboard.writeText(text);
  log("转写文本已复制");
}

function bindEvents() {
  els.startButton.addEventListener("click", submitJob);
  for (const button of els.sourceModeButtons) {
    button.addEventListener("click", () => setSourceMode(button.dataset.sourceMode));
  }
  els.startCaptureButton.addEventListener("click", startCapture);
  els.stopCaptureButton.addEventListener("click", stopCapture);
  els.launchBrowserButton.addEventListener("click", launchDebugBrowser);
  els.refreshBrowserTabsButton.addEventListener("click", loadBrowserTabs);
  els.probeBrowserButton.addEventListener("click", probeBrowserMedia);
  els.transcribeBrowserButton.addEventListener("click", transcribeBrowserMedia);
  els.browserCandidateSelect.addEventListener("change", updateBrowserCandidateState);
  els.reloadButton.addEventListener("click", loadAll);
  els.refreshFilesButton.addEventListener("click", loadAll);
  els.engineSelect.addEventListener("change", updateModelNote);
  els.saveTerminologyButton.addEventListener("click", saveTerminology);
  els.copyTranscriptButton.addEventListener("click", copyTranscript);
  els.audioFileInput.addEventListener("change", () => {
    const file = els.audioFileInput.files[0];
    els.dropTitle.textContent = file ? file.name : "拖入或选择 MP3/MP4";
    els.dropSubtitle.textContent = file ? `${formatBytes(file.size)} · 准备提交` : "也可以直接转写 input 目录中的文件";
  });
  for (const eventName of ["dragenter", "dragover"]) {
    els.dropZone.addEventListener(eventName, (event) => {
      event.preventDefault();
      els.dropZone.classList.add("is-dragging");
    });
  }
  for (const eventName of ["dragleave", "drop"]) {
    els.dropZone.addEventListener(eventName, () => {
      els.dropZone.classList.remove("is-dragging");
    });
  }
}

function formatTime(value) {
  const total = Math.max(0, Math.floor(Number(value) || 0));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  if (hours) {
    return `${hours}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
  }
  return `${minutes}:${String(seconds).padStart(2, "0")}`;
}

function formatBytes(bytes) {
  if (!bytes) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  let value = bytes;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index += 1;
  }
  return `${value.toFixed(index ? 1 : 0)} ${units[index]}`;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function cleanError(error) {
  try {
    const parsed = JSON.parse(error.message);
    return parsed.detail || error.message;
  } catch {
    return error.message;
  }
}

initUiSelects();
bindEvents();
loadAll().catch((error) => log(`初始化失败: ${cleanError(error)}`));
setInterval(refreshJobs, 5000);
setInterval(() => {
  if (
    state.activeCapture &&
    ["starting", "recording", "stopping", "transcribing"].includes(state.activeCapture.status)
  ) {
    refreshCapture(state.activeCapture.id).catch(() => {});
  }
}, 2000);
setInterval(() => {
  if (state.activeBrowserImport && state.activeBrowserImport.status === "extracting") {
    refreshBrowserImport(state.activeBrowserImport.id).catch(() => {});
  }
}, 2000);
