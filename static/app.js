const state = {
  models: [],
  files: [],
  jobs: [],
  activeJob: null,
  eventSource: null,
};

const TERMINAL_STATUSES = new Set(["completed", "failed", "cancelled"]);

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

async function loadAll() {
  const [health, models, files, terms, jobs] = await Promise.all([
    api("/api/health"),
    api("/api/models"),
    api("/api/files"),
    api("/api/terminology"),
    api("/api/jobs"),
  ]);
  els.serverStatus.textContent = health.ok ? "本地服务运行中" : "服务异常";
  state.models = models.models;
  state.files = files.files;
  state.jobs = jobs.jobs;
  renderCounts();
  renderModels();
  renderFiles();
  renderTerminology(terms.terms);
  renderQueue();
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
    button.innerHTML = `<strong>${escapeHtml(job.source_name)}</strong><span>${job.status} · ${Math.round(job.progress * 100)}%</span>`;
    button.addEventListener("click", () => setActiveJob(job.id));
    els.jobQueue.appendChild(button);
  }
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

bindEvents();
loadAll().catch((error) => log(`初始化失败: ${cleanError(error)}`));
setInterval(refreshJobs, 5000);
