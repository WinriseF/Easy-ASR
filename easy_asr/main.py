from __future__ import annotations

import asyncio
import threading
import uuid
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from easy_asr.browser_debug import (
    DEFAULT_BROWSER_HOME_URL,
    DEFAULT_DEBUG_ENDPOINT,
    DebugBrowserManager,
)
from easy_asr.capture import PlaybackCaptureManager
from easy_asr.debug_runtime import flush_logging, get_logger, log_debug, shorten
from easy_asr.engines.base import EngineOptions
from easy_asr.jobs import JobManager, event_payload, parse_formats


def _runtime_base_dir() -> Path:
    """
    运行时可写目录：
    input / output / chunks / models / data 都放这里。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _resource_base_dir() -> Path:
    """
    打包资源目录：
    static 等只读资源放这里。
    """
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent / "_internal"))
    return Path(__file__).resolve().parent.parent


BASE_DIR = _runtime_base_dir()
RESOURCE_DIR = _resource_base_dir()
STATIC_DIR = RESOURCE_DIR / "static"
LOGGER = get_logger(__name__)

manager = JobManager(BASE_DIR)
manager.ensure_dirs()
capture_manager = PlaybackCaptureManager(BASE_DIR, manager)
capture_manager.ensure_dirs()
browser_manager = DebugBrowserManager(BASE_DIR, manager)
browser_manager.ensure_dirs()

app = FastAPI(title="Easy-ASR", version="0.1.0")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
model_download_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="model-download")
model_download_lock = threading.RLock()
model_downloads: dict[str, dict] = {}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health() -> dict:
    return {
        "ok": True,
        "base_dir": str(BASE_DIR),
        "input_dir": str(manager.input_dir),
        "output_dir": str(manager.output_root),
    }


@app.get("/api/models")
def models() -> dict:
    return {"models": [descriptor.__dict__ for descriptor in manager.registry.descriptors()]}


@app.get("/api/models/downloads/{download_id}")
def model_download(download_id: str) -> dict:
    with model_download_lock:
        record = model_downloads.get(download_id)
        if record is None:
            raise HTTPException(status_code=404, detail="model download not found")
        return dict(record)


@app.post("/api/models/preload")
def preload_model(
    engine_id: Annotated[str, Form()] = "funasr-sensevoice",
    model_name: Annotated[str, Form()] = "",
    cpu_threads: Annotated[int, Form()] = 4,
    compute_type: Annotated[str, Form()] = "int8",
) -> dict:
    options = EngineOptions(
        engine_id=engine_id,
        model_name=model_name,
        cpu_threads=max(1, min(int(cpu_threads), 32)),
        compute_type=compute_type,
    )
    download_id = uuid.uuid4().hex[:12]
    record = {
        "id": download_id,
        "status": "queued",
        "engine_id": options.engine_id,
        "model_name": options.model_name or _default_model_name(options.engine_id),
        "error": "",
    }
    with model_download_lock:
        model_downloads[download_id] = record
    model_download_executor.submit(_run_model_preload, download_id, options)
    return dict(record)


@app.get("/api/files")
def files() -> dict:
    return {"files": manager.list_input_files()}


@app.get("/api/jobs")
def jobs() -> dict:
    return {"jobs": manager.list_jobs()}


@app.get("/api/capture/devices")
def capture_devices() -> dict:
    return capture_manager.devices_payload()


@app.get("/api/capture/sessions")
def capture_sessions() -> dict:
    return {"sessions": capture_manager.list_sessions()}


@app.get("/api/capture/{session_id}")
def capture_session(session_id: str) -> dict:
    record = capture_manager.get_session(session_id)
    if record is None:
        raise HTTPException(status_code=404, detail="capture session not found")
    return record.snapshot()


@app.get("/api/browser/tabs")
def browser_tabs(endpoint: str = DEFAULT_DEBUG_ENDPOINT) -> dict:
    try:
        log_debug(LOGGER, "api_browser_tabs_start", endpoint=endpoint)
        payload = {"available": True, "install_hint": "", "tabs": browser_manager.list_tabs(endpoint)}
        log_debug(LOGGER, "api_browser_tabs_ok", endpoint=endpoint, tab_count=len(payload["tabs"]))
        return payload
    except Exception as exc:
        LOGGER.exception("api_browser_tabs_failed")
        flush_logging()
        return {"available": False, "install_hint": str(exc), "tabs": []}


@app.get("/api/browser/imports")
def browser_imports() -> dict:
    return {"imports": browser_manager.list_imports()}


@app.get("/api/browser/imports/{import_id}")
def browser_import(import_id: str) -> dict:
    record = browser_manager.get_import(import_id)
    if record is None:
        raise HTTPException(status_code=404, detail="browser import not found")
    return record.snapshot()


@app.get("/api/jobs/{job_id}")
def job(job_id: str) -> dict:
    record = manager.get_job(job_id)
    if record is None:
        raise HTTPException(status_code=404, detail="job not found")
    return record.snapshot()


@app.post("/api/jobs")
def create_job(
    file: Annotated[UploadFile | None, File()] = None,
    existing_file: Annotated[str, Form()] = "",
    engine_id: Annotated[str, Form()] = "funasr-sensevoice",
    model_name: Annotated[str, Form()] = "",
    language: Annotated[str, Form()] = "zh",
    chunk_seconds: Annotated[int, Form()] = 600,
    batch_size_s: Annotated[int, Form()] = 60,
    merge_length_s: Annotated[int, Form()] = 15,
    cpu_threads: Annotated[int, Form()] = 4,
    compute_type: Annotated[str, Form()] = "int8",
    whisper_preset: Annotated[str, Form()] = "balanced",
    apply_terminology: Annotated[bool, Form()] = True,
    transcript_mode: Annotated[str, Form()] = "whole",
    output_formats: Annotated[str, Form()] = "txt,srt,vtt,tsv,json",
) -> dict:
    try:
        if file is not None and file.filename:
            input_path = manager.save_upload(file.filename, file.file)
        elif existing_file:
            input_path = manager.resolve_input_file(existing_file)
        else:
            raise ValueError("请上传文件或选择 input 目录中的文件。")

        options = EngineOptions(
            engine_id=engine_id,
            model_name=model_name,
            language=language,
            chunk_seconds=max(30, min(int(chunk_seconds), 3600)),
            batch_size_s=max(10, min(int(batch_size_s), 300)),
            merge_length_s=max(5, min(int(merge_length_s), 60)),
            cpu_threads=max(1, min(int(cpu_threads), 32)),
            compute_type=compute_type,
            whisper_preset=whisper_preset if whisper_preset in {"fast", "balanced", "quality"} else "balanced",
            apply_terminology=apply_terminology,
            transcript_mode=_transcript_mode(transcript_mode),
        )
        record = manager.submit(input_path, options, parse_formats(output_formats))
        return record.snapshot()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        if file is not None:
            file.file.close()


@app.post("/api/browser/probe")
def probe_browser(
    endpoint: Annotated[str, Form()] = DEFAULT_DEBUG_ENDPOINT,
    tab_id: Annotated[str, Form()] = "",
    listen_seconds: Annotated[int, Form()] = 4,
    reload_page: Annotated[bool, Form()] = False,
) -> dict:
    try:
        log_debug(
            LOGGER,
            "api_browser_probe_start",
            endpoint=endpoint,
            tab_id=tab_id,
            listen_seconds=listen_seconds,
            reload_page=reload_page,
        )
        payload = browser_manager.probe_tab(
            endpoint=endpoint,
            tab_id=tab_id,
            listen_seconds=max(1, min(int(listen_seconds), 15)),
            reload_page=reload_page,
        )
        log_debug(
            LOGGER,
            "api_browser_probe_ok",
            endpoint=endpoint,
            tab_id=tab_id,
            candidate_count=len(payload.get("candidates") or []),
            recommended_url=shorten(payload.get("recommended_url", ""), 200),
        )
        return payload
    except Exception as exc:
        LOGGER.exception("api_browser_probe_failed")
        flush_logging()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/browser/launch")
def launch_browser(
    endpoint: Annotated[str, Form()] = DEFAULT_DEBUG_ENDPOINT,
    start_url: Annotated[str, Form()] = DEFAULT_BROWSER_HOME_URL,
) -> dict:
    try:
        log_debug(LOGGER, "api_browser_launch_start", endpoint=endpoint, start_url=start_url)
        payload = browser_manager.launch_browser(endpoint=endpoint, start_url=start_url)
        log_debug(
            LOGGER,
            "api_browser_launch_ok",
            endpoint=endpoint,
            start_url=start_url,
            already_running=payload.get("already_running"),
            executable=payload.get("executable", ""),
            tab_count=len(payload.get("tabs") or []),
        )
        return payload
    except Exception as exc:
        LOGGER.exception("api_browser_launch_failed")
        flush_logging()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/browser/transcribe")
def transcribe_browser(
    endpoint: Annotated[str, Form()] = DEFAULT_DEBUG_ENDPOINT,
    tab_id: Annotated[str, Form()] = "",
    source_url: Annotated[str, Form()] = "",
    engine_id: Annotated[str, Form()] = "funasr-sensevoice",
    model_name: Annotated[str, Form()] = "",
    language: Annotated[str, Form()] = "zh",
    chunk_seconds: Annotated[int, Form()] = 600,
    batch_size_s: Annotated[int, Form()] = 60,
    merge_length_s: Annotated[int, Form()] = 15,
    cpu_threads: Annotated[int, Form()] = 4,
    compute_type: Annotated[str, Form()] = "int8",
    whisper_preset: Annotated[str, Form()] = "balanced",
    apply_terminology: Annotated[bool, Form()] = True,
    transcript_mode: Annotated[str, Form()] = "whole",
    output_formats: Annotated[str, Form()] = "txt,srt,vtt,tsv,json",
) -> dict:
    try:
        log_debug(
            LOGGER,
            "api_browser_transcribe_start",
            endpoint=endpoint,
            tab_id=tab_id,
            source_url=shorten(source_url, 500),
            engine_id=engine_id,
            model_name=model_name,
            language=language,
            compute_type=compute_type,
        )
        options = EngineOptions(
            engine_id=engine_id,
            model_name=model_name,
            language=language,
            chunk_seconds=max(30, min(int(chunk_seconds), 3600)),
            batch_size_s=max(10, min(int(batch_size_s), 300)),
            merge_length_s=max(5, min(int(merge_length_s), 60)),
            cpu_threads=max(1, min(int(cpu_threads), 32)),
            compute_type=compute_type,
            whisper_preset=whisper_preset if whisper_preset in {"fast", "balanced", "quality"} else "balanced",
            apply_terminology=apply_terminology,
            transcript_mode=_transcript_mode(transcript_mode),
        )
        record = browser_manager.start_transcribe(endpoint, tab_id, source_url, options, parse_formats(output_formats))
        log_debug(
            LOGGER,
            "api_browser_transcribe_queued",
            import_id=record.id,
            endpoint=endpoint,
            tab_id=tab_id,
            source_url=shorten(source_url, 500),
            media_path=record.media_path,
        )
        return record.snapshot()
    except Exception as exc:
        LOGGER.exception("api_browser_transcribe_failed")
        flush_logging()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/browser/download")
def download_browser_media(
    endpoint: Annotated[str, Form()] = DEFAULT_DEBUG_ENDPOINT,
    tab_id: Annotated[str, Form()] = "",
    source_url: Annotated[str, Form()] = "",
    kind: Annotated[str, Form()] = "audio",
) -> dict:
    try:
        log_debug(
            LOGGER,
            "api_browser_download_start",
            endpoint=endpoint,
            tab_id=tab_id,
            source_url=shorten(source_url, 500),
            kind=kind,
        )
        record = browser_manager.start_download(endpoint, tab_id, source_url, kind)
        log_debug(
            LOGGER,
            "api_browser_download_queued",
            import_id=record.id,
            endpoint=endpoint,
            tab_id=tab_id,
            source_url=shorten(source_url, 500),
            kind=kind,
            media_path=record.media_path,
        )
        return record.snapshot()
    except Exception as exc:
        LOGGER.exception("api_browser_download_failed")
        flush_logging()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/capture/start")
def start_capture(
    device_index: Annotated[str, Form()] = "",
    engine_id: Annotated[str, Form()] = "funasr-sensevoice",
    model_name: Annotated[str, Form()] = "",
    language: Annotated[str, Form()] = "zh",
    chunk_seconds: Annotated[int, Form()] = 600,
    batch_size_s: Annotated[int, Form()] = 60,
    merge_length_s: Annotated[int, Form()] = 15,
    cpu_threads: Annotated[int, Form()] = 4,
    compute_type: Annotated[str, Form()] = "int8",
    whisper_preset: Annotated[str, Form()] = "balanced",
    apply_terminology: Annotated[bool, Form()] = True,
    transcript_mode: Annotated[str, Form()] = "whole",
    output_formats: Annotated[str, Form()] = "txt,srt,json",
) -> dict:
    try:
        options = EngineOptions(
            engine_id=engine_id,
            model_name=model_name,
            language=language,
            chunk_seconds=max(30, min(int(chunk_seconds), 3600)),
            batch_size_s=max(10, min(int(batch_size_s), 300)),
            merge_length_s=max(5, min(int(merge_length_s), 60)),
            cpu_threads=max(1, min(int(cpu_threads), 32)),
            compute_type=compute_type,
            whisper_preset=whisper_preset if whisper_preset in {"fast", "balanced", "quality"} else "balanced",
            apply_terminology=apply_terminology,
            transcript_mode=_transcript_mode(transcript_mode),
        )
        parsed_device_index = int(device_index) if device_index.strip() else None
        record = capture_manager.start(options, parse_formats(output_formats), parsed_device_index)
        return record.snapshot()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/capture/{session_id}/stop")
def stop_capture(session_id: str) -> dict:
    try:
        record = capture_manager.stop(session_id)
        return record.snapshot()
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="capture session not found") from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/capture/{session_id}/events")
async def capture_events(session_id: str) -> StreamingResponse:
    if capture_manager.get_session(session_id) is None:
        raise HTTPException(status_code=404, detail="capture session not found")

    async def stream():
        offset = 0
        while True:
            record = capture_manager.get_session(session_id)
            if record is None:
                break
            events = record.events[offset:]
            for event in events:
                yield f"data: {event_payload(event)}\n\n"
            offset += len(events)
            if record.status in {"completed", "failed"} and offset >= len(record.events):
                break
            await asyncio.sleep(0.2)

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.get("/api/jobs/{job_id}/events")
async def job_events(job_id: str) -> StreamingResponse:
    if manager.get_job(job_id) is None:
        raise HTTPException(status_code=404, detail="job not found")

    async def stream():
        offset = 0
        while True:
            record = manager.get_job(job_id)
            if record is None:
                break
            events = record.events[offset:]
            for event in events:
                yield f"data: {event_payload(event)}\n\n"
            offset += len(events)
            if record.status in {"completed", "failed", "cancelled"} and offset >= len(record.events):
                break
            await asyncio.sleep(0.2)

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.get("/api/jobs/{job_id}/download/{format_name}")
def download(job_id: str, format_name: str) -> FileResponse:
    record = manager.get_job(job_id)
    if record is None:
        raise HTTPException(status_code=404, detail="job not found")
    path = record.outputs.get(format_name)
    if path is None or not path.exists():
        raise HTTPException(status_code=404, detail="output not found")
    return FileResponse(path, filename=path.name)


@app.get("/api/browser/imports/{import_id}/download")
def download_browser_import(import_id: str) -> FileResponse:
    record = browser_manager.get_import(import_id)
    if record is None:
        raise HTTPException(status_code=404, detail="browser import not found")
    if not record.media_path.exists():
        raise HTTPException(status_code=404, detail="media output not found")
    return FileResponse(record.media_path, filename=record.media_path.name)


def _transcript_mode(value: str) -> str:
    return value if value in {"whole", "chunk", "sentence"} else "whole"


def _default_model_name(engine_id: str) -> str:
    for descriptor in manager.registry.descriptors():
        if descriptor.id == engine_id:
            return descriptor.default_model
    return ""


def _run_model_preload(download_id: str, options: EngineOptions) -> None:
    with model_download_lock:
        if download_id in model_downloads:
            model_downloads[download_id]["status"] = "running"
    try:
        manager.registry.preload(options)
        with model_download_lock:
            model_downloads[download_id]["status"] = "completed"
    except Exception as exc:
        LOGGER.exception("model_preload_failed")
        flush_logging()
        with model_download_lock:
            if download_id in model_downloads:
                model_downloads[download_id]["status"] = "failed"
                model_downloads[download_id]["error"] = str(exc)


@app.get("/api/terminology")
def get_terminology() -> dict:
    library = manager.terminology_store.load()
    return {"terms": library.to_dicts(), "prompt": library.prompt_text()}


@app.put("/api/terminology")
async def put_terminology(payload: dict) -> dict:
    raw_terms = payload.get("terms")
    if not isinstance(raw_terms, list):
        raise HTTPException(status_code=400, detail="terms must be a list")
    library = manager.update_terminology(raw_terms)
    return {"terms": library.to_dicts(), "prompt": library.prompt_text()}
