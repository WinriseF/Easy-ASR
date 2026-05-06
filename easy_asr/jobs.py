from __future__ import annotations

import json
import re
import shutil
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from easy_asr.audio import probe_duration
from easy_asr.engines import EngineRegistry
from easy_asr.engines.base import EngineOptions
from easy_asr.output import write_outputs
from easy_asr.terminology import TerminologyLibrary, TerminologyStore


AUDIO_EXTS = {".mp3", ".m4a", ".wav", ".flac", ".aac", ".ogg", ".mp4", ".mkv"}
TERMINAL_STATUSES = {"completed", "failed", "cancelled"}


@dataclass
class JobEvent:
    at: str
    type: str
    message: str
    progress: float | None = None


@dataclass
class JobRecord:
    id: str
    source_name: str
    input_path: Path
    status: str
    progress: float
    options: EngineOptions
    formats: set[str]
    created_at: str
    updated_at: str
    output_dir: Path
    outputs: dict[str, Path] = field(default_factory=dict)
    segments: list[dict] = field(default_factory=list)
    duration_seconds: float | None = None
    error: str = ""
    hidden: bool = False
    events: list[JobEvent] = field(default_factory=list)

    def snapshot(self) -> dict:
        data = asdict(self)
        data["input_path"] = str(self.input_path)
        data["output_dir"] = str(self.output_dir)
        data["outputs"] = {key: str(path) for key, path in self.outputs.items()}
        data["formats"] = sorted(self.formats)
        data["options"]["work_dir"] = str(self.options.work_dir) if self.options.work_dir else None
        return data

    def summary(self) -> dict:
        return {
            "id": self.id,
            "source_name": self.source_name,
            "status": self.status,
            "progress": self.progress,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "duration_seconds": self.duration_seconds,
            "error": self.error,
            "formats": sorted(self.formats),
            "engine_id": self.options.engine_id,
            "output_formats": sorted(self.outputs),
        }


class JobManager:
    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
        self.input_dir = base_dir / "input"
        self.output_root = base_dir / "output" / "jobs"
        self.work_root = base_dir / "chunks" / "_jobs"
        self.terminology_store = TerminologyStore(base_dir / "data" / "terminology" / "default.json")
        self.registry = EngineRegistry(base_dir)
        self._jobs: dict[str, JobRecord] = {}
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="easy-asr")

    def ensure_dirs(self) -> None:
        self.input_dir.mkdir(parents=True, exist_ok=True)
        self.output_root.mkdir(parents=True, exist_ok=True)
        self.work_root.mkdir(parents=True, exist_ok=True)
        self.terminology_store.ensure_default()

    def list_input_files(self) -> list[dict]:
        self.input_dir.mkdir(parents=True, exist_ok=True)
        files = []
        for path in sorted(self.input_dir.iterdir()):
            if path.is_file() and path.suffix.lower() in AUDIO_EXTS:
                files.append(
                    {
                        "name": path.name,
                        "path": str(path),
                        "size": path.stat().st_size,
                        "duration_seconds": probe_duration(path),
                    }
                )
        return files

    def save_upload(self, filename: str, stream) -> Path:
        self.input_dir.mkdir(parents=True, exist_ok=True)
        safe_name = sanitize_filename(filename)
        if Path(safe_name).suffix.lower() not in AUDIO_EXTS:
            raise ValueError("只支持音频或视频文件: " + ", ".join(sorted(AUDIO_EXTS)))
        target = self.input_dir / safe_name
        if target.exists():
            target = self.input_dir / f"{target.stem}_{short_timestamp()}{target.suffix}"
        with target.open("wb") as handle:
            shutil.copyfileobj(stream, handle)
        return target

    def resolve_input_file(self, name: str) -> Path:
        candidate = (self.input_dir / name).resolve()
        input_root = self.input_dir.resolve()
        if input_root not in candidate.parents and candidate != input_root:
            raise ValueError("输入文件必须位于 input 目录内。")
        if not candidate.exists() or not candidate.is_file():
            raise FileNotFoundError(name)
        if candidate.suffix.lower() not in AUDIO_EXTS:
            raise ValueError("不支持的文件类型。")
        return candidate

    def submit(self, input_path: Path, options: EngineOptions, formats: set[str], hidden: bool = False) -> JobRecord:
        job_id = uuid.uuid4().hex[:12]
        now = iso_now()
        options.work_dir = self.work_root / job_id
        duration_seconds = probe_duration(input_path)
        record = JobRecord(
            id=job_id,
            source_name=input_path.name,
            input_path=input_path,
            status="queued",
            progress=0.0,
            options=options,
            formats=formats or {"txt"},
            created_at=now,
            updated_at=now,
            output_dir=self.output_root / job_id,
            duration_seconds=duration_seconds,
            hidden=hidden,
        )
        queued_message = "任务已加入队列"
        if duration_seconds is not None:
            queued_message = f"{queued_message}，音频时长 {_duration_label(duration_seconds)}"
        record.events.append(JobEvent(at=now, type="queued", message=queued_message, progress=0))
        with self._lock:
            self._jobs[job_id] = record
        self._executor.submit(self._run, job_id)
        return record

    def list_jobs(self) -> list[dict]:
        with self._lock:
            return [
                job.summary()
                for job in sorted(self._jobs.values(), key=lambda item: item.created_at, reverse=True)
                if not job.hidden
            ]

    def get_job(self, job_id: str) -> JobRecord | None:
        with self._lock:
            return self._jobs.get(job_id)

    def update_terminology(self, raw_terms: list[dict]) -> TerminologyLibrary:
        library = TerminologyLibrary.from_dicts(raw_terms)
        self.terminology_store.save(library)
        return library

    def _run(self, job_id: str) -> None:
        job = self.get_job(job_id)
        if job is None:
            return
        try:
            self._set_status(job, "running", 0.01, "开始处理任务")
            terminology = self.terminology_store.load() if job.options.apply_terminology else TerminologyLibrary()
            engine = self.registry.create(job.options.engine_id)
            result = engine.transcribe(job.input_path, job.options, terminology, lambda p, m: self._progress(job, p, m))
            outputs = write_outputs(job.output_dir, job.source_name, result, job.formats)
            with self._lock:
                job.outputs = outputs
                job.segments = [segment.to_dict() for segment in result.segments]
                job.duration_seconds = result.duration_seconds
            self._progress(job, 0.98, "正在清理临时切片")
            self._cleanup_work_dir(job)
            self._set_status(job, "completed", 1.0, "转写完成")
        except Exception as exc:
            with self._lock:
                job.error = str(exc)
            self._progress(job, 0.98, "正在清理临时切片")
            self._cleanup_work_dir(job)
            self._set_status(job, "failed", 1.0, f"处理失败: {exc}")

    def _progress(self, job: JobRecord, progress: float, message: str) -> None:
        with self._lock:
            job.progress = max(0.0, min(0.99, progress))
            job.updated_at = iso_now()
            job.events.append(JobEvent(at=job.updated_at, type="progress", message=message, progress=job.progress))

    def _set_status(self, job: JobRecord, status: str, progress: float, message: str) -> None:
        with self._lock:
            job.status = status
            job.progress = progress
            job.updated_at = iso_now()
            job.events.append(JobEvent(at=job.updated_at, type=status, message=message, progress=progress))

    def _cleanup_work_dir(self, job: JobRecord) -> None:
        work_dir = job.options.work_dir
        if work_dir is None:
            return
        try:
            resolved_work_dir = work_dir.resolve()
            resolved_root = self.work_root.resolve()
            if resolved_work_dir == resolved_root or resolved_root not in resolved_work_dir.parents:
                raise ValueError(f"refusing to clean unexpected work dir: {resolved_work_dir}")
            if resolved_work_dir.exists():
                shutil.rmtree(resolved_work_dir)
                message = "临时切片已清理"
            else:
                message = "临时切片无需清理"
        except Exception as exc:
            message = f"临时切片清理失败: {exc}"
        with self._lock:
            job.updated_at = iso_now()
            job.events.append(JobEvent(at=job.updated_at, type="cleanup", message=message, progress=job.progress))


def parse_formats(value: str) -> set[str]:
    values = {item.strip().lower() for item in value.split(",") if item.strip()}
    allowed = {"txt", "srt", "json"}
    return values & allowed or {"txt"}


def sanitize_filename(filename: str) -> str:
    name = Path(filename or "audio.mp3").name
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(". ")
    return name or f"audio_{short_timestamp()}.mp3"


def short_timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _duration_label(duration_seconds: float) -> str:
    total = max(0, int(duration_seconds))
    hours = total // 3600
    minutes = (total % 3600) // 60
    seconds = total % 60
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def event_payload(event: JobEvent) -> str:
    return json.dumps(asdict(event), ensure_ascii=False)
