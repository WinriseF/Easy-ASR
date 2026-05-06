from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from easy_asr.engines.base import EngineOptions
from easy_asr.jobs import JobManager, event_payload, parse_formats


BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"

manager = JobManager(BASE_DIR)
manager.ensure_dirs()

app = FastAPI(title="Easy-ASR", version="0.1.0")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


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


@app.get("/api/files")
def files() -> dict:
    return {"files": manager.list_input_files()}


@app.get("/api/jobs")
def jobs() -> dict:
    return {"jobs": manager.list_jobs()}


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
    output_formats: Annotated[str, Form()] = "txt,srt,json",
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
        )
        record = manager.submit(input_path, options, parse_formats(output_formats))
        return record.snapshot()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        if file is not None:
            file.file.close()


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
            await asyncio.sleep(1)

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
