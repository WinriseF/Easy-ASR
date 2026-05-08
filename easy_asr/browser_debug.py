from __future__ import annotations

import json
import os
import re
import math
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from shutil import which
from typing import Any
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from easy_asr.audio import ffmpeg_exe, ffprobe_exe, probe_duration, require_ffmpeg
from easy_asr.debug_runtime import flush_logging, get_logger, log_debug, log_warning, shorten
from easy_asr.engines.base import EngineOptions
from easy_asr.jobs import JobManager, safe_path_stem, short_timestamp


DEFAULT_DEBUG_ENDPOINT = "http://127.0.0.1:9222"
DEFAULT_BROWSER_HOME_URL = "https://www.bing.com"
INSTALL_HINT = "请先安装 requirements-browser-debug.txt，然后重启本地服务。"
MEDIA_URL_RE = re.compile(
    r"(\.m3u8|\.mpd|\.mp4|\.m4a|\.mp3|\.aac|\.wav|\.flac|\.ogg|\.oga|\.webm|\.ts|\.m4s)(?:[?#]|$)",
    re.IGNORECASE,
)
MEDIA_HINT_RE = re.compile(r"(m3u8|mpd|manifest|playlist|videoplayback|audio|video|hls|dash)", re.IGNORECASE)
MEDIA_MIME_RE = re.compile(r"(audio|video|mpegurl|dash\+xml|mp4|webm|ogg)", re.IGNORECASE)
NON_MEDIA_ENDPOINT_RE = re.compile(
    r"(drm|widevine|license|licence|token|auth|key|beacon|telemetry|analytics|log|stat|track)",
    re.IGNORECASE,
)
YTDLP_HOST_RE = re.compile(
    r"(^|\.)("
    r"youtube\.com|youtu\.be|bilibili\.com|vimeo\.com|tiktok\.com|douyin\.com|kuaishou\.com|"
    r"twitter\.com|x\.com|facebook\.com|instagram\.com|twitch\.tv|dailymotion\.com|"
    r"reddit\.com|soundcloud\.com|nicovideo\.jp"
    r")$",
    re.IGNORECASE,
)
LOGGER = get_logger(__name__)


MEDIA_PROBE_SCRIPT = r"""
(() => {
  const toUrl = (value) => {
    if (!value) return "";
    try { return new URL(value, location.href).href; } catch { return String(value); }
  };
  const media = Array.from(document.querySelectorAll("video,audio")).map((el, index) => ({
    index,
    tag: el.tagName.toLowerCase(),
    currentSrc: toUrl(el.currentSrc),
    src: toUrl(el.src),
    srcAttr: toUrl(el.getAttribute("src") || ""),
    duration: Number.isFinite(el.duration) ? el.duration : null,
    currentTime: Number.isFinite(el.currentTime) ? el.currentTime : 0,
    playbackRate: Number.isFinite(el.playbackRate) ? el.playbackRate : 1,
    paused: Boolean(el.paused),
    muted: Boolean(el.muted),
    readyState: el.readyState,
    networkState: el.networkState,
    videoWidth: el.videoWidth || null,
    videoHeight: el.videoHeight || null,
    sources: Array.from(el.querySelectorAll("source")).map((source, sourceIndex) => ({
      index: sourceIndex,
      src: toUrl(source.src || source.getAttribute("src") || ""),
      type: source.type || "",
    })),
  }));
  const resources = performance.getEntriesByType("resource").map((entry) => ({
    name: toUrl(entry.name),
    initiatorType: entry.initiatorType || "",
    duration: Number.isFinite(entry.duration) ? entry.duration : null,
    transferSize: Number.isFinite(entry.transferSize) ? entry.transferSize : null,
  }));
  return {
    title: document.title,
    url: location.href,
    media,
    resources,
  };
})()
"""


@dataclass
class BrowserImportEvent:
    at: str
    type: str
    message: str
    progress: float | None = None


@dataclass
class BrowserImportRecord:
    id: str
    status: str
    source_url: str
    tab_id: str
    endpoint: str
    created_at: str
    updated_at: str
    media_path: Path
    job_id: str = ""
    mode: str = "transcribe"
    duration_seconds: float | None = None
    error: str = ""
    events: list[BrowserImportEvent] = field(default_factory=list)

    def snapshot(self) -> dict:
        data = asdict(self)
        data["media_path"] = str(self.media_path)
        return data

    def summary(self) -> dict:
        return {
            "id": self.id,
            "status": self.status,
            "source_url": self.source_url,
            "tab_id": self.tab_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "media_path": str(self.media_path),
            "job_id": self.job_id,
            "duration_seconds": self.duration_seconds,
            "error": self.error,
        }


class DebugBrowserManager:
    def __init__(self, base_dir: Path, job_manager: JobManager):
        self.base_dir = base_dir
        self.job_manager = job_manager
        self.media_dir = base_dir / "input" / "browser_media"
        self._imports: dict[str, BrowserImportRecord] = {}
        self._options: dict[str, EngineOptions] = {}
        self._formats: dict[str, set[str]] = {}
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="browser-media")

    def ensure_dirs(self) -> None:
        self.media_dir.mkdir(parents=True, exist_ok=True)

    def launch_browser(
        self,
        endpoint: str = DEFAULT_DEBUG_ENDPOINT,
        start_url: str = DEFAULT_BROWSER_HOME_URL,
    ) -> dict:
        endpoint = _normalize_endpoint(endpoint)
        _ensure_local_endpoint(endpoint)
        log_debug(LOGGER, "browser_launch_requested", endpoint=endpoint, start_url=start_url)
        try:
            version = _debug_json(endpoint, "/json/version")
            log_debug(LOGGER, "browser_launch_reused_existing", endpoint=endpoint, browser=version.get("Browser", ""))
            return {
                "available": True,
                "already_running": True,
                "endpoint": endpoint,
                "browser": str(version.get("Browser") or ""),
                "executable": "",
                "profile_dir": "",
                "tabs": self.list_tabs(endpoint),
            }
        except RuntimeError:
            pass

        executable = _find_browser_executable()
        profile_dir = self.base_dir / "data" / "debug-browser-profile"
        profile_dir.mkdir(parents=True, exist_ok=True)
        port = _endpoint_port(endpoint)
        cmd = [
            executable,
            f"--remote-debugging-port={port}",
            "--remote-debugging-address=127.0.0.1",
            "--remote-allow-origins=*",
            f"--user-data-dir={profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--new-window",
            start_url.strip() or DEFAULT_BROWSER_HOME_URL,
        ]
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        log_debug(LOGGER, "browser_launch_spawned", endpoint=endpoint, executable=executable, profile_dir=profile_dir, cmd=cmd)
        flush_logging()
        deadline = time.monotonic() + 10
        last_error = ""
        while time.monotonic() < deadline:
            try:
                version = _debug_json(endpoint, "/json/version")
                return {
                    "available": True,
                    "already_running": False,
                    "endpoint": endpoint,
                    "browser": str(version.get("Browser") or ""),
                    "executable": executable,
                    "profile_dir": str(profile_dir),
                    "tabs": self.list_tabs(endpoint),
                }
            except RuntimeError as exc:
                last_error = str(exc)
                time.sleep(0.5)
        raise RuntimeError(f"调试浏览器启动后没有响应: {last_error or endpoint}")

    def list_tabs(self, endpoint: str = DEFAULT_DEBUG_ENDPOINT) -> list[dict]:
        tabs = _debug_json(endpoint, "/json/list")
        if not isinstance(tabs, list):
            raise RuntimeError("调试浏览器返回了异常标签页数据。")
        pages = []
        for tab in tabs:
            if tab.get("type") != "page":
                continue
            pages.append(
                {
                    "id": str(tab.get("id", "")),
                    "title": str(tab.get("title", "")),
                    "url": str(tab.get("url", "")),
                    "webSocketDebuggerUrl": str(tab.get("webSocketDebuggerUrl", "")),
                }
            )
        return pages

    def probe_tab(
        self,
        endpoint: str = DEFAULT_DEBUG_ENDPOINT,
        tab_id: str = "",
        listen_seconds: float = 4.0,
        reload_page: bool = False,
    ) -> dict:
        tab = self._find_tab(endpoint, tab_id)
        log_debug(
            LOGGER,
            "browser_probe_started",
            endpoint=endpoint,
            tab_id=tab.get("id", ""),
            tab_title=tab.get("title", ""),
            tab_url=tab.get("url", ""),
            listen_seconds=listen_seconds,
            reload_page=reload_page,
        )
        with CdpSession(tab["webSocketDebuggerUrl"]) as session:
            session.call("Runtime.enable")
            session.call("Network.enable")
            if reload_page:
                session.call("Page.enable")
                session.call("Page.reload", {"ignoreCache": True})
            before = _evaluate_media(session)
            events = session.drain_events(max(0.5, min(float(listen_seconds), 15.0)))
            after = _evaluate_media(session)
            network_items = _network_candidates(events)
        candidates = _merge_candidates(before, after, network_items)
        candidates = self._validate_candidates(endpoint, tab, candidates)
        candidates = self._with_extractor_candidate(tab, after or before, candidates)
        recommended = next((item for item in candidates if item["supported"]), None)
        if recommended is not None:
            recommended["recommended"] = True
        log_debug(
            LOGGER,
            "browser_probe_completed",
            endpoint=endpoint,
            tab_id=tab.get("id", ""),
            candidate_count=len(candidates),
            recommended=self._candidate_log_payload(recommended) if recommended else None,
            candidates=[self._candidate_log_payload(item) for item in candidates[:8]],
        )
        return {
            "tab": {key: tab.get(key, "") for key in ("id", "title", "url")},
            "page": after or before,
            "candidates": candidates,
            "recommended_url": recommended["url"] if recommended else "",
            "notes": _probe_notes(before, after, candidates),
        }

    def _with_extractor_candidate(self, tab: dict, page: dict, candidates: list[dict]) -> list[dict]:
        page_url = str(page.get("url") or tab.get("url") or "")
        if not _is_ytdlp_page_url(page_url):
            return candidates
        candidate = _yt_dlp_candidate(page_url, str(tab.get("title") or page.get("title") or "YouTube"))
        if any(item.get("url") == candidate["url"] and item.get("source") == candidate["source"] for item in candidates):
            return candidates
        return sorted([candidate, *candidates], key=lambda item: (0 if item.get("supported") else 1, -int(item.get("score") or 0)))

    def _validate_candidates(self, endpoint: str, tab: dict, candidates: list[dict]) -> list[dict]:
        if not candidates:
            return []
        workers = min(6, len(candidates))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="media-probe") as executor:
            validated = list(
                executor.map(lambda candidate: self._validate_candidate(endpoint, tab, dict(candidate)), candidates)
            )
        return sorted(
            validated,
            key=lambda item: (
                0 if item.get("supported") else 1,
                -(float(item.get("duration_seconds") or 0)),
                -int(item.get("score") or 0),
            ),
        )

    def _validate_candidate(self, endpoint: str, tab: dict, candidate: dict) -> dict:
        url = str(candidate.get("url") or "")
        try:
            _validate_media_url(url)
            headers = self._headers_for_media(endpoint, tab, url)
            duration_seconds = _probe_remote_duration(url, headers)
            candidate["duration_seconds"] = duration_seconds
            candidate["supported"] = duration_seconds > 0
            candidate["validation_status"] = "ok" if candidate["supported"] else "failed"
            if candidate["supported"]:
                candidate["reason"] = ""
            else:
                candidate["reason"] = "媒体源可访问但没有可用时长。"
        except Exception as exc:
            candidate["duration_seconds"] = 0
            candidate["supported"] = False
            candidate["validation_status"] = "failed"
            candidate["reason"] = _short_error(exc)
        log_debug(
            LOGGER,
            "browser_candidate_validated",
            endpoint=endpoint,
            tab_id=tab.get("id", ""),
            candidate=self._candidate_log_payload(candidate),
        )
        return candidate

    def list_imports(self) -> list[dict]:
        with self._lock:
            return [
                item.summary()
                for item in sorted(self._imports.values(), key=lambda record: record.created_at, reverse=True)
            ]

    def get_import(self, import_id: str) -> BrowserImportRecord | None:
        with self._lock:
            return self._imports.get(import_id)

    def start_transcribe(
        self,
        endpoint: str,
        tab_id: str,
        source_url: str,
        options: EngineOptions,
        formats: set[str],
    ) -> BrowserImportRecord:
        _validate_media_url(source_url)
        tab = self._find_tab(endpoint, tab_id)
        import_id = uuid.uuid4().hex[:12]
        now = iso_now()
        source_label = str(tab.get("title") or _candidate_title(source_url) or "browser_media")
        media_stem = safe_path_stem(source_label, "browser_media")
        record = BrowserImportRecord(
            id=import_id,
            status="extracting",
            source_url=source_url,
            tab_id=str(tab["id"]),
            endpoint=endpoint,
            created_at=now,
            updated_at=now,
            media_path=self.media_dir / f"{media_stem}_{short_timestamp()}_{import_id[:8]}.wav",
            mode="transcribe",
        )
        record.events.append(BrowserImportEvent(at=now, type="extracting", message="正在提取浏览器原始媒体音频", progress=0))
        with self._lock:
            self._imports[import_id] = record
            self._options[import_id] = options
            self._formats[import_id] = formats or {"txt"}
        log_debug(
            LOGGER,
            "browser_transcribe_record_created",
            import_id=import_id,
            endpoint=endpoint,
            tab_id=tab.get("id", ""),
            source_url=shorten(source_url, 500),
            media_path=record.media_path,
            options=options.__dict__,
            formats=sorted(self._formats[import_id]),
        )
        self._executor.submit(self._extract_and_submit, import_id)
        return record

    def start_download(
        self,
        endpoint: str,
        tab_id: str,
        source_url: str,
        kind: str,
    ) -> BrowserImportRecord:
        kind = "video" if kind == "video" else "audio"
        _validate_media_url(source_url)
        tab = self._find_tab(endpoint, tab_id)
        import_id = uuid.uuid4().hex[:12]
        now = iso_now()
        source_label = str(tab.get("title") or _candidate_title(source_url) or "browser_media")
        media_stem = safe_path_stem(source_label, "browser_media")
        suffix = ".mp4" if kind == "video" else ".m4a"
        record = BrowserImportRecord(
            id=import_id,
            status="extracting",
            source_url=source_url,
            tab_id=str(tab["id"]),
            endpoint=endpoint,
            created_at=now,
            updated_at=now,
            media_path=self.media_dir / f"{media_stem}_{short_timestamp()}_{import_id[:8]}{suffix}",
            mode=f"download_{kind}",
        )
        record.events.append(
            BrowserImportEvent(at=now, type="extracting", message=f"正在提取浏览器媒体{_download_kind_label(kind)}", progress=0)
        )
        with self._lock:
            self._imports[import_id] = record
        log_debug(
            LOGGER,
            "browser_download_record_created",
            import_id=import_id,
            endpoint=endpoint,
            tab_id=tab.get("id", ""),
            source_url=shorten(source_url, 500),
            mode=record.mode,
            media_path=record.media_path,
        )
        self._executor.submit(self._extract_and_submit, import_id)
        return record

    def _find_tab(self, endpoint: str, tab_id: str) -> dict:
        tabs = self.list_tabs(endpoint)
        if not tabs:
            raise RuntimeError("没有发现调试浏览器标签页。")
        if tab_id:
            for tab in tabs:
                if tab["id"] == tab_id:
                    return tab
            raise RuntimeError("没有找到指定的调试浏览器标签页。")
        return tabs[0]

    def _extract_and_submit(self, import_id: str) -> None:
        record = self.get_import(import_id)
        if record is None:
            return
        try:
            tab = self._find_tab(record.endpoint, record.tab_id)
            log_debug(
                LOGGER,
                "browser_extract_started",
                import_id=import_id,
                mode=record.mode,
                endpoint=record.endpoint,
                tab_id=record.tab_id,
                tab_title=tab.get("title", ""),
                source_url=shorten(record.source_url, 500),
                is_ytdlp_page=_is_ytdlp_page_url(record.source_url),
                output_path=record.media_path,
            )
            flush_logging()
            if record.mode in {"download_audio", "download_video"}:
                kind = "video" if record.mode == "download_video" else "audio"
                if _is_ytdlp_page_url(record.source_url):
                    log_debug(LOGGER, "browser_extract_branch", import_id=import_id, branch="download_with_ytdlp", kind=kind)
                    self._download_with_ytdlp(record, kind)
                else:
                    headers = self._headers_for_media(record.endpoint, tab, record.source_url)
                    log_debug(
                        LOGGER,
                        "browser_extract_branch",
                        import_id=import_id,
                        branch="download_direct_media",
                        kind=kind,
                        headers=_sanitize_headers(headers),
                    )
                    self._download_direct_media(record, headers, kind)
                with self._lock:
                    record.status = "downloaded"
                    record.updated_at = iso_now()
                    record.events.append(
                        BrowserImportEvent(
                            at=record.updated_at,
                            type="downloaded",
                            message=f"浏览器媒体{_download_kind_label(kind)}已准备下载",
                            progress=1,
                        )
                    )
                return

            if _is_ytdlp_page_url(record.source_url):
                log_debug(LOGGER, "browser_extract_branch", import_id=import_id, branch="extract_audio_with_ytdlp")
                self._extract_audio_with_ytdlp(record)
            else:
                headers = self._headers_for_media(record.endpoint, tab, record.source_url)
                log_debug(
                    LOGGER,
                    "browser_extract_branch",
                    import_id=import_id,
                    branch="extract_audio_direct",
                    headers=_sanitize_headers(headers),
                )
                self._extract_audio(record, headers)
            duration_label = _duration_label(record.duration_seconds)
            options = self._options.get(import_id) or EngineOptions()
            formats = self._formats.get(import_id) or {"txt"}
            job = self.job_manager.submit(record.media_path, options, formats)
            with self._lock:
                record.status = "submitted"
                record.job_id = job.id
                record.updated_at = iso_now()
                record.events.append(
                    BrowserImportEvent(
                        at=record.updated_at,
                        type="submitted",
                        message=f"浏览器媒体已提交转写队列，音频时长 {duration_label}",
                        progress=1,
                    )
                )
        except Exception as exc:
            LOGGER.exception("browser_extract_failed | import_id=%s", import_id)
            flush_logging()
            with self._lock:
                record.status = "failed"
                record.error = str(exc)
                record.updated_at = iso_now()
                record.events.append(
                    BrowserImportEvent(at=record.updated_at, type="failed", message=f"浏览器媒体提取失败: {exc}", progress=1)
            )

    def _download_with_ytdlp(self, record: BrowserImportRecord, kind: str) -> None:
        require_ffmpeg()
        record.media_path.parent.mkdir(parents=True, exist_ok=True)
        output_template = str(record.media_path.with_suffix(".%(ext)s"))
        tab = self._find_tab(record.endpoint, record.tab_id)
        headers = self._headers_for_media(record.endpoint, tab, record.source_url)
        ffmpeg_dir = os.environ.get("EASY_ASR_FFMPEG_DIR", "")
        log_debug(
            LOGGER,
            "ytdlp_download_start",
            import_id=record.id,
            kind=kind,
            source_url=shorten(record.source_url, 500),
            media_path=record.media_path,
            output_template=output_template,
            ffmpeg_dir=ffmpeg_dir,
            headers=_sanitize_headers(headers),
            execution="subprocess",
        )
        flush_logging()
        self._run_ytdlp_worker(record, mode="download", kind=kind, output_template=output_template, headers=headers)
        if not record.media_path.exists() or record.media_path.stat().st_size == 0:
            raise RuntimeError("yt-dlp 没有生成可用的媒体文件。")
        log_debug(
            LOGGER,
            "ytdlp_download_finished",
            import_id=record.id,
            kind=kind,
            media_path=record.media_path,
            size=record.media_path.stat().st_size,
        )
        self._set_download_duration(record)

    def _download_direct_media(self, record: BrowserImportRecord, headers: dict[str, str], kind: str) -> None:
        require_ffmpeg()
        record.media_path.parent.mkdir(parents=True, exist_ok=True)
        header_text = "".join(f"{key}: {value}\r\n" for key, value in headers.items())
        cmd = [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y"]
        if header_text:
            cmd.extend(["-headers", header_text])
        cmd.extend(["-i", record.source_url])
        if kind == "video":
            cmd.extend(["-map", "0:v:0?", "-map", "0:a:0?", "-c", "copy"])
        else:
            cmd.extend(["-vn", "-ac", "2", "-c:a", "aac", "-b:a", "192k"])
        cmd.append(str(record.media_path))
        log_debug(
            LOGGER,
            "direct_media_download_start",
            import_id=record.id,
            kind=kind,
            source_url=shorten(record.source_url, 500),
            headers=_sanitize_headers(headers),
            cmd=cmd,
        )
        flush_logging()
        _run_ffmpeg(cmd)
        if not record.media_path.exists() or record.media_path.stat().st_size == 0:
            raise RuntimeError("ffmpeg 没有生成可用的媒体文件。")
        log_debug(
            LOGGER,
            "direct_media_download_finished",
            import_id=record.id,
            kind=kind,
            media_path=record.media_path,
            size=record.media_path.stat().st_size,
        )
        self._set_download_duration(record)

    def _set_download_duration(self, record: BrowserImportRecord) -> None:
        duration_seconds = probe_duration(record.media_path)
        with self._lock:
            record.duration_seconds = duration_seconds
            record.updated_at = iso_now()

    def _extract_audio_with_ytdlp(self, record: BrowserImportRecord) -> None:
        require_ffmpeg()
        record.media_path.parent.mkdir(parents=True, exist_ok=True)
        output_template = str(record.media_path.with_suffix(".%(ext)s"))
        tab = self._find_tab(record.endpoint, record.tab_id)
        headers = self._headers_for_media(record.endpoint, tab, record.source_url)
        log_debug(
            LOGGER,
            "ytdlp_extract_start",
            import_id=record.id,
            source_url=shorten(record.source_url, 500),
            media_path=record.media_path,
            output_template=output_template,
            ffmpeg_dir=os.environ.get("EASY_ASR_FFMPEG_DIR", ""),
            headers=_sanitize_headers(headers),
            execution="subprocess",
        )
        flush_logging()
        self._run_ytdlp_worker(record, mode="extract_audio", kind="audio", output_template=output_template, headers=headers)
        if not record.media_path.exists() or record.media_path.stat().st_size == 0:
            raise RuntimeError("yt-dlp 没有生成可用的音频文件。")
        log_debug(
            LOGGER,
            "ytdlp_extract_finished",
            import_id=record.id,
            media_path=record.media_path,
            size=record.media_path.stat().st_size,
        )
        duration_seconds = probe_duration(record.media_path)
        duration_label = _duration_label(duration_seconds)
        with self._lock:
            record.duration_seconds = duration_seconds
            record.updated_at = iso_now()
            record.events.append(
                BrowserImportEvent(
                    at=record.updated_at,
                    type="extracted",
                    message=f"页面媒体音频已提取，音频时长 {duration_label}",
                    progress=0.7,
                )
            )

    def _run_ytdlp_worker(
        self,
        record: BrowserImportRecord,
        mode: str,
        kind: str,
        output_template: str,
        headers: dict[str, str],
    ) -> None:
        config_path = record.media_path.with_suffix(f".{record.id}.ytdlp.json")
        config = {
            "import_id": record.id,
            "mode": mode,
            "kind": kind,
            "source_url": record.source_url,
            "media_path": str(record.media_path),
            "output_template": output_template,
            "ffmpeg_dir": os.environ.get("EASY_ASR_FFMPEG_DIR", ""),
            "headers": headers,
        }
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        cmd = _ytdlp_worker_command(config_path)
        log_debug(
            LOGGER,
            "ytdlp_worker_subprocess_start",
            import_id=record.id,
            mode=mode,
            kind=kind,
            cmd=cmd,
            config_path=config_path,
            config_size=config_path.stat().st_size,
        )
        flush_logging()
        try:
            completed = subprocess.run(
                cmd,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        finally:
            try:
                config_path.unlink(missing_ok=True)
            except Exception as exc:
                log_warning(LOGGER, "ytdlp_worker_config_cleanup_failed", import_id=record.id, error=str(exc))
        log_debug(
            LOGGER,
            "ytdlp_worker_subprocess_completed",
            import_id=record.id,
            returncode=completed.returncode,
            stdout=shorten(completed.stdout.strip(), 2000),
            stderr=shorten(completed.stderr.strip(), 4000),
            media_exists=record.media_path.exists(),
            media_size=record.media_path.stat().st_size if record.media_path.exists() else 0,
        )
        flush_logging()
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or f"yt-dlp worker exited with {completed.returncode}"
            raise RuntimeError(f"yt-dlp 子进程失败，退出码 {completed.returncode}: {shorten(detail, 1200)}")

    def _headers_for_media(self, endpoint: str, tab: dict, source_url: str) -> dict[str, str]:
        headers: dict[str, str] = {
            "Referer": str(tab.get("url") or ""),
        }
        try:
            version = _debug_json(endpoint, "/json/version")
            user_agent = str(version.get("User-Agent") or "")
            if user_agent:
                headers["User-Agent"] = user_agent
        except Exception:
            pass

        try:
            with CdpSession(tab["webSocketDebuggerUrl"]) as session:
                session.call("Network.enable")
                user_agent = session.call(
                    "Runtime.evaluate",
                    {"expression": "navigator.userAgent", "returnByValue": True},
                ).get("result", {}).get("value")
                if user_agent:
                    headers["User-Agent"] = str(user_agent)
                cookie_result = session.call("Network.getCookies", {"urls": [source_url, str(tab.get("url") or "")]})
                cookies = cookie_result.get("cookies") or []
                cookie_header = "; ".join(
                    f"{cookie.get('name')}={cookie.get('value')}"
                    for cookie in cookies
                    if cookie.get("name") and cookie.get("value") is not None
                )
                if cookie_header:
                    headers["Cookie"] = cookie_header
        except Exception:
            pass
        filtered = {key: value for key, value in headers.items() if value}
        log_debug(
            LOGGER,
            "browser_media_headers_built",
            endpoint=endpoint,
            tab_id=tab.get("id", ""),
            source_url=shorten(source_url, 400),
            headers=_sanitize_headers(filtered),
        )
        return filtered

    def _extract_audio(self, record: BrowserImportRecord, headers: dict[str, str]) -> None:
        require_ffmpeg()
        record.media_path.parent.mkdir(parents=True, exist_ok=True)
        header_text = "".join(f"{key}: {value}\r\n" for key, value in headers.items())
        cmd = [
            ffmpeg_exe(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
        ]
        if header_text:
            cmd.extend(["-headers", header_text])
        cmd.extend(
            [
                "-i",
                record.source_url,
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "pcm_s16le",
                str(record.media_path),
            ]
        )
        log_debug(
            LOGGER,
            "direct_audio_extract_start",
            import_id=record.id,
            source_url=shorten(record.source_url, 500),
            headers=_sanitize_headers(headers),
            cmd=cmd,
        )
        flush_logging()
        completed = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        log_debug(
            LOGGER,
            "direct_audio_extract_completed",
            import_id=record.id,
            returncode=completed.returncode,
            stdout=shorten(completed.stdout.strip(), 800),
            stderr=shorten(completed.stderr.strip(), 800),
        )
        flush_logging()
        if completed.returncode != 0:
            message = completed.stderr.strip() or completed.stdout.strip() or "ffmpeg 提取失败"
            raise RuntimeError(message)
        if not record.media_path.exists() or record.media_path.stat().st_size == 0:
            raise RuntimeError("ffmpeg 没有生成可用的音频文件。")
        duration_seconds = probe_duration(record.media_path)
        duration_label = _duration_label(duration_seconds)
        with self._lock:
            record.duration_seconds = duration_seconds
            record.updated_at = iso_now()
            record.events.append(
                BrowserImportEvent(
                    at=record.updated_at,
                    type="extracted",
                    message=f"原始媒体音频已提取，音频时长 {duration_label}",
                    progress=0.7,
                )
            )

    def _candidate_log_payload(self, candidate: dict | None) -> dict | None:
        if not candidate:
            return None
        return {
            "url": shorten(candidate.get("url", ""), 260),
            "source": candidate.get("source", ""),
            "title": shorten(candidate.get("title", ""), 160),
            "supported": candidate.get("supported"),
            "recommended": candidate.get("recommended"),
            "validation_status": candidate.get("validation_status", ""),
            "duration_seconds": candidate.get("duration_seconds"),
            "reason": shorten(candidate.get("reason", ""), 260),
            "score": candidate.get("score"),
        }


class CdpSession:
    def __init__(self, websocket_url: str):
        self.websocket_url = websocket_url
        self._next_id = 1
        self._ws = None

    def __enter__(self) -> "CdpSession":
        websocket = _load_websocket()
        self._ws = websocket.create_connection(self.websocket_url, timeout=8, suppress_origin=True)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._ws is not None:
            self._ws.close()
            self._ws = None

    def call(self, method: str, params: dict | None = None) -> dict:
        if self._ws is None:
            raise RuntimeError("CDP session is not connected")
        self._ws.settimeout(8)
        message_id = self._next_id
        self._next_id += 1
        self._ws.send(json.dumps({"id": message_id, "method": method, "params": params or {}}))
        while True:
            message = json.loads(self._ws.recv())
            if message.get("id") != message_id:
                continue
            if "error" in message:
                raise RuntimeError(message["error"].get("message") or str(message["error"]))
            return message.get("result") or {}

    def drain_events(self, seconds: float) -> list[dict]:
        if self._ws is None:
            raise RuntimeError("CDP session is not connected")
        events: list[dict] = []
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            remaining = max(0.1, deadline - time.monotonic())
            self._ws.settimeout(remaining)
            try:
                message = json.loads(self._ws.recv())
            except Exception:
                break
            if "method" in message:
                events.append(message)
        return events


def _debug_json(endpoint: str, path: str) -> Any:
    endpoint = _normalize_endpoint(endpoint)
    url = endpoint.rstrip("/") + path
    request = Request(url, headers={"Accept": "application/json"})
    try:
        with urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))
    except URLError as exc:
        raise RuntimeError(f"无法连接调试浏览器: {url}") from exc


def _normalize_endpoint(endpoint: str) -> str:
    value = (endpoint or DEFAULT_DEBUG_ENDPOINT).strip()
    if not value.startswith(("http://", "https://")):
        value = "http://" + value
    return value.rstrip("/")


def _endpoint_port(endpoint: str) -> int:
    parsed = urlparse(endpoint)
    return int(parsed.port or 9222)


def _ensure_local_endpoint(endpoint: str) -> None:
    host = (urlparse(endpoint).hostname or "").lower()
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("自动启动调试浏览器只支持本机 127.0.0.1/localhost 调试端口。")


def _find_browser_executable() -> str:
    names = ["chrome.exe", "chrome", "msedge.exe", "msedge"]
    for name in names:
        path = which(name)
        if path:
            return path

    candidates = []
    for env_name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        root = os.environ.get(env_name)
        if not root:
            continue
        root_path = Path(root)
        candidates.extend(
            [
                root_path / "Google" / "Chrome" / "Application" / "chrome.exe",
                root_path / "Microsoft" / "Edge" / "Application" / "msedge.exe",
            ]
        )
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    raise RuntimeError("没有找到 Chrome 或 Edge，可手动安装后重试，或自行用 --remote-debugging-port=9222 启动。")


def _load_websocket():
    try:
        import websocket
    except ImportError as exc:
        raise RuntimeError(INSTALL_HINT) from exc
    return websocket


def _evaluate_media(session: CdpSession) -> dict:
    result = session.call(
        "Runtime.evaluate",
        {
            "expression": MEDIA_PROBE_SCRIPT,
            "returnByValue": True,
            "awaitPromise": True,
        },
    )
    value = result.get("result", {}).get("value")
    return value if isinstance(value, dict) else {}


def _network_candidates(events: list[dict]) -> list[dict]:
    candidates: list[dict] = []
    for event in events:
        if event.get("method") != "Network.responseReceived":
            continue
        params = event.get("params") or {}
        response = params.get("response") or {}
        url = str(response.get("url") or "")
        mime_type = str(response.get("mimeType") or "")
        if _looks_like_media(url, mime_type):
            candidates.append(
                {
                    "url": url,
                    "source": "network",
                    "mime_type": mime_type,
                    "resource_type": str(params.get("type") or ""),
                    "title": _candidate_title(url),
                }
            )
    return candidates


def _merge_candidates(before: dict, after: dict, network_items: list[dict]) -> list[dict]:
    raw: list[dict] = []
    for page in (before, after):
        raw.extend(_media_element_candidates(page))
        raw.extend(_performance_candidates(page))
    raw.extend(network_items)

    deduped: dict[str, dict] = {}
    for item in raw:
        url = str(item.get("url") or "")
        if not url:
            continue
        normalized_url = _without_fragment(url)
        candidate = _candidate_payload(item, normalized_url)
        existing = deduped.get(normalized_url)
        if existing is None or candidate["score"] > existing["score"]:
            deduped[normalized_url] = candidate
    candidates = sorted(deduped.values(), key=lambda item: item["score"], reverse=True)
    return candidates


def _media_element_candidates(page: dict) -> list[dict]:
    candidates: list[dict] = []
    for media in page.get("media") or []:
        urls = [
            ("currentSrc", media.get("currentSrc")),
            ("src", media.get("src")),
            ("srcAttr", media.get("srcAttr")),
        ]
        for source in media.get("sources") or []:
            urls.append(("source", source.get("src")))
        for source, url in urls:
            if not url:
                continue
            candidates.append(
                {
                    "url": url,
                    "source": f"media_element:{source}",
                    "mime_type": "",
                    "resource_type": media.get("tag", ""),
                    "title": f"{media.get('tag', 'media')} playbackRate={media.get('playbackRate', 1)}",
                    "playback_rate": media.get("playbackRate", 1),
                }
            )
    return candidates


def _performance_candidates(page: dict) -> list[dict]:
    candidates: list[dict] = []
    for resource in page.get("resources") or []:
        url = str(resource.get("name") or "")
        initiator = str(resource.get("initiatorType") or "")
        if _looks_like_media(url, "") or initiator in {"audio", "video"}:
            candidates.append(
                {
                    "url": url,
                    "source": "performance",
                    "mime_type": "",
                    "resource_type": initiator,
                    "title": _candidate_title(url),
                }
            )
    return candidates


def _candidate_payload(item: dict, url: str) -> dict:
    parsed = urlparse(url)
    mime_type = str(item.get("mime_type") or "")
    supported = (
        parsed.scheme in {"http", "https"}
        and _looks_like_media(url, mime_type)
        and not _is_non_media_endpoint(url, mime_type)
    )
    reason = ""
    if parsed.scheme in {"blob", "data"}:
        reason = "blob/data 源无法直接提交，需要从网络请求里找到真实媒体分片。"
    elif parsed.scheme not in {"http", "https"}:
        reason = "只支持 http/https 媒体 URL。"
    elif _is_non_media_endpoint(url, mime_type):
        reason = "这是 token/license/drm/key 等控制端点，不是可转写音频源。"
    elif not _looks_like_media(url, mime_type):
        reason = "看起来不像可直接交给 ffmpeg 的媒体 URL。"
    return {
        "id": uuid.uuid5(uuid.NAMESPACE_URL, url).hex[:12],
        "url": url,
        "source": str(item.get("source") or ""),
        "mime_type": mime_type,
        "resource_type": str(item.get("resource_type") or ""),
        "title": str(item.get("title") or _candidate_title(url)),
        "supported": supported,
        "recommended": False,
        "reason": reason,
        "duration_seconds": 0,
        "validation_status": "pending",
        "score": _candidate_score(url, item, supported),
    }


def _yt_dlp_candidate(page_url: str, title: str) -> dict:
    reason = ""
    supported = True
    try:
        _load_ytdlp()
    except Exception as exc:
        supported = False
        reason = str(exc)
    return {
        "id": uuid.uuid5(uuid.NAMESPACE_URL, f"yt-dlp:{page_url}").hex[:12],
        "url": page_url,
        "source": "yt-dlp",
        "mime_type": "",
        "resource_type": "page",
        "title": title or _candidate_title(page_url),
        "supported": supported,
        "recommended": False,
        "reason": reason,
        "duration_seconds": 0,
        "validation_status": "ok" if supported else "failed",
        "score": 220,
    }


def _is_ytdlp_page_url(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not YTDLP_HOST_RE.search(host):
        return False
    if MEDIA_URL_RE.search(parsed.path.lower()):
        return False
    if host.endswith(("youtube.com", "youtu.be")):
        return parsed.path.startswith(("/watch", "/shorts/", "/live/")) or host.endswith("youtu.be")
    return bool(parsed.path.strip("/"))


def _load_ytdlp():
    try:
        import yt_dlp as ytdlp
    except ImportError as exc:
        raise RuntimeError("平台页面提取需要安装 requirements-browser-debug.txt 中的 yt-dlp。") from exc
    return ytdlp


def _ytdlp_worker_command(config_path: Path) -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--easy-asr-ytdlp", str(config_path)]
    run_app = Path(__file__).resolve().parent.parent / "run_app.py"
    return [sys.executable, str(run_app), "--easy-asr-ytdlp", str(config_path)]


def _run_ffmpeg(cmd: list[str]) -> None:
    log_debug(LOGGER, "ffmpeg_subprocess_start", cmd=cmd)
    flush_logging()
    completed = subprocess.run(
        cmd,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    log_debug(
        LOGGER,
        "ffmpeg_subprocess_completed",
        cmd=cmd,
        returncode=completed.returncode,
        stdout=shorten(completed.stdout.strip(), 800),
        stderr=shorten(completed.stderr.strip(), 800),
    )
    flush_logging()
    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip() or "ffmpeg 处理失败"
        raise RuntimeError(message)


def _download_kind_label(kind: str) -> str:
    return "视频" if kind == "video" else "音频"


def _candidate_score(url: str, item: dict, supported: bool) -> int:
    if _is_non_media_endpoint(url, str(item.get("mime_type") or "")):
        return 1
    source = str(item.get("source") or "")
    lower = url.lower()
    if not supported:
        if lower.startswith(("blob:", "data:")):
            return 8
        return 5
    if ".m3u8" in lower or ".mpd" in lower:
        return 140
    if source.startswith("media_element"):
        return 125
    if re.search(r"\.(mp4|m4a|mp3|aac|wav|flac|ogg|oga|webm)(?:[?#]|$)", lower):
        return 115
    if "videoplayback" in lower:
        return 110
    if MEDIA_MIME_RE.search(str(item.get("mime_type") or "")):
        return 100
    if re.search(r"\.(m4s|ts)(?:[?#]|$)", lower):
        return 70
    if MEDIA_URL_RE.search(lower):
        return 80
    if source == "network":
        return 60
    return 50


def _looks_like_media(url: str, mime_type: str) -> bool:
    lower = url.lower()
    return bool(MEDIA_URL_RE.search(lower) or MEDIA_HINT_RE.search(lower) or MEDIA_MIME_RE.search(mime_type or ""))


def _is_non_media_endpoint(url: str, mime_type: str) -> bool:
    if MEDIA_MIME_RE.search(mime_type or ""):
        return False
    parsed = urlparse(url)
    identity = f"{parsed.netloc}{parsed.path}"
    if MEDIA_URL_RE.search(identity):
        return False
    return bool(NON_MEDIA_ENDPOINT_RE.search(identity))


def _without_fragment(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.fragment:
        return url
    return parsed._replace(fragment="").geturl()


def _candidate_title(url: str) -> str:
    path = urlparse(url).path
    name = Path(path).name
    return name or urlparse(url).netloc or "media"


def _probe_remote_duration(source_url: str, headers: dict[str, str]) -> float:
    ffprobe_path = ffprobe_exe()
    header_text = "".join(f"{key}: {value}\r\n" for key, value in headers.items())
    cmd = [
        ffprobe_path,
        "-v",
        "error",
    ]
    if header_text:
        cmd.extend(["-headers", header_text])
    cmd.extend(
        [
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            source_url,
        ]
    )
    try:
        log_debug(
            LOGGER,
            "ffprobe_remote_duration_start",
            source_url=shorten(source_url, 500),
            headers=_sanitize_headers(headers),
            cmd=cmd,
        )
        flush_logging()
        completed = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
        )
    except subprocess.TimeoutExpired as exc:
        LOGGER.exception("ffprobe_remote_duration_timeout")
        flush_logging()
        raise RuntimeError("ffprobe 验证超时") from exc
    log_debug(
        LOGGER,
        "ffprobe_remote_duration_completed",
        source_url=shorten(source_url, 500),
        returncode=completed.returncode,
        stdout=shorten(completed.stdout.strip(), 800),
        stderr=shorten(completed.stderr.strip(), 800),
    )
    flush_logging()
    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip() or "ffprobe 无法读取媒体源"
        raise RuntimeError(message)
    value = completed.stdout.strip()
    try:
        duration_seconds = float(value)
    except ValueError as exc:
        raise RuntimeError("ffprobe 没有返回可用时长") from exc
    if not math.isfinite(duration_seconds) or duration_seconds <= 0:
        raise RuntimeError("ffprobe 返回的媒体时长无效")
    return duration_seconds


def _short_error(error: Exception) -> str:
    text = str(error).strip().replace("\r", " ").replace("\n", " ")
    return text[:180] or error.__class__.__name__


def _sanitize_headers(headers: dict[str, str]) -> dict[str, str]:
    sanitized: dict[str, str] = {}
    for key, value in headers.items():
        if key.lower() == "cookie":
            sanitized[key] = f"<cookie len={len(value)}>"
        else:
            sanitized[key] = shorten(value, 220)
    return sanitized


def _module_summary(module: Any) -> dict[str, str]:
    if module is None:
        return {"present": "false"}
    return {
        "present": "true",
        "file": shorten(getattr(module, "__file__", ""), 260),
        "version": str(getattr(module, "__version__", "")),
    }


def _yt_dlp_runtime_summary(ytdlp: Any) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "module_file": shorten(getattr(ytdlp, "__file__", ""), 260),
        "module_version": str(getattr(ytdlp, "__version__", "")),
    }
    try:
        from yt_dlp import dependencies as deps

        summary["dependencies"] = {
            name: _module_summary(getattr(deps, name, None))
            for name in ["certifi", "curl_cffi", "requests", "urllib3", "websockets", "brotli", "Cryptodome"]
        }
    except Exception as exc:
        summary["dependencies_error"] = str(exc)
    return summary


def _summarize_ytdlp_options(options: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for key, value in options.items():
        if key in {"logger", "progress_hooks"}:
            summary[key] = f"<{key}>"
            continue
        summary[key] = shorten(value, 260)
    return summary


def _make_ytdlp_progress_hook(import_id: str):
    def hook(payload: dict[str, Any]) -> None:
        log_debug(
            LOGGER,
            "ytdlp_progress",
            import_id=import_id,
            status=payload.get("status", ""),
            downloaded_bytes=payload.get("downloaded_bytes"),
            total_bytes=payload.get("total_bytes"),
            total_bytes_estimate=payload.get("total_bytes_estimate"),
            eta=payload.get("eta"),
            speed=payload.get("speed"),
            filename=shorten(payload.get("filename", ""), 260),
        )
        flush_logging()

    return hook


class _YtdlpDebugLogger:
    def __init__(self, import_id: str):
        self.import_id = import_id

    def debug(self, message: str) -> None:
        log_debug(LOGGER, "ytdlp_debug", import_id=self.import_id, message=shorten(message, 1200))
        flush_logging()

    def warning(self, message: str) -> None:
        log_warning(LOGGER, "ytdlp_warning", import_id=self.import_id, message=shorten(message, 1200))
        flush_logging()

    def error(self, message: str) -> None:
        log_warning(LOGGER, "ytdlp_error", import_id=self.import_id, message=shorten(message, 1200))
        flush_logging()


def _validate_media_url(source_url: str) -> None:
    parsed = urlparse(source_url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("只支持 http/https 原始媒体 URL。")
    if not parsed.netloc:
        raise ValueError("原始媒体 URL 缺少主机名。")


def _probe_notes(before: dict, after: dict, candidates: list[dict]) -> list[str]:
    notes: list[str] = []
    usable_count = sum(1 for item in candidates if item.get("supported"))
    failed_count = sum(1 for item in candidates if item.get("validation_status") == "failed")
    if candidates:
        notes.append(f"已实际验证 {len(candidates)} 个媒体候选，可用 {usable_count} 个，失败 {failed_count} 个。")
    media = (after or before).get("media") or []
    if any(str(item.get("currentSrc") or "").startswith("blob:") for item in media):
        notes.append("页面媒体是 blob: 源；如果候选里没有 m3u8/mpd/mp4，请先刷新或重新播放后再探测。")
    if any(item.get("source") == "yt-dlp" for item in candidates):
        notes.append("检测到可由 yt-dlp 处理的平台页面；已加入页面提取候选，适合处理 blob/MSE 自适应流。")
    if any(float(item.get("playbackRate") or 1) != 1 for item in media):
        notes.append("页面正在倍速播放；候选媒体会按原始 URL 转写，不使用倍速后的系统声音。")
    if not candidates:
        notes.append("没有发现可直接转写的媒体源。可刷新页面并延长监听时间，或回退系统音频采集。")
    return notes


def _duration_label(duration_seconds: float | None) -> str:
    if duration_seconds is None:
        return "未知"
    total = max(0, int(duration_seconds))
    hours = total // 3600
    minutes = (total % 3600) // 60
    seconds = total % 60
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()
