from __future__ import annotations

import json
import re
import shutil
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from easy_asr.debug_runtime import flush_logging, get_logger, log_debug, log_warning
from easy_asr.audio import probe_duration
from easy_asr.engines import EngineRegistry
from easy_asr.engines.base import EngineOptions
from easy_asr.output import write_outputs
from easy_asr.terminology import TerminologyLibrary, TerminologyStore


AUDIO_EXTS = {".mp3", ".m4a", ".wav", ".flac", ".aac", ".ogg", ".mp4", ".mkv"}
TERMINAL_STATUSES = {"completed", "failed", "cancelled"}
JOB_METADATA_FILE = "_job.json"
OUTPUT_EXTS = {"txt", "srt", "vtt", "tsv", "csv", "json"}
LOGGER = get_logger(__name__)


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
            "output_dir": str(self.output_dir),
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
        self._load_history()

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

    def submit(
        self,
        input_path: Path,
        options: EngineOptions,
        formats: set[str],
        hidden: bool = False,
        source_name: str = "",
    ) -> JobRecord:
        job_id = uuid.uuid4().hex[:12]
        now = iso_now()
        options.work_dir = self.work_root / job_id
        duration_seconds = probe_duration(input_path)
        source_name = display_source_name(source_name, input_path) if source_name else input_path.name
        record = JobRecord(
            id=job_id,
            source_name=source_name,
            input_path=input_path,
            status="queued",
            progress=0.0,
            options=options,
            formats=formats or {"txt"},
            created_at=now,
            updated_at=now,
            output_dir=self._output_dir_for(source_name, job_id),
            duration_seconds=duration_seconds,
            hidden=hidden,
        )
        queued_message = "任务已加入队列"
        if duration_seconds is not None:
            queued_message = f"{queued_message}，音频时长 {_duration_label(duration_seconds)}"
        record.events.append(JobEvent(at=now, type="queued", message=queued_message, progress=0))
        with self._lock:
            self._jobs[job_id] = record
            self._save_job(record)
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
            log_debug(
                LOGGER,
                "job_run_start",
                job_id=job.id,
                input_path=job.input_path,
                engine_id=job.options.engine_id,
                model_name=job.options.model_name,
                work_dir=job.options.work_dir,
                formats=sorted(job.formats),
            )
            flush_logging()
            terminology = self.terminology_store.load() if job.options.apply_terminology else TerminologyLibrary()
            engine = self.registry.create(job.options.engine_id)
            log_debug(LOGGER, "job_engine_created", job_id=job.id, engine_id=job.options.engine_id, engine_type=type(engine).__name__)
            flush_logging()
            result = engine.transcribe(job.input_path, job.options, terminology, lambda p, m: self._progress(job, p, m))
            outputs = write_outputs(job.output_dir, job.source_name, result, job.formats)
            with self._lock:
                job.outputs = outputs
                job.segments = [segment.to_dict() for segment in result.segments]
                job.duration_seconds = result.duration_seconds
                self._save_job(job)
            log_debug(
                LOGGER,
                "job_transcribe_completed",
                job_id=job.id,
                segment_count=len(result.segments),
                duration_seconds=result.duration_seconds,
                outputs={key: str(path) for key, path in outputs.items()},
            )
            flush_logging()
            self._progress(job, 0.98, "正在清理临时切片")
            self._cleanup_work_dir(job)
            self._set_status(job, "completed", 1.0, "转写完成")
        except Exception as exc:
            log_warning(LOGGER, "job_run_failed", job_id=job.id, error=repr(exc))
            flush_logging()
            with self._lock:
                job.error = str(exc)
                self._save_job(job)
            self._progress(job, 0.98, "正在清理临时切片")
            self._cleanup_work_dir(job)
            self._set_status(job, "failed", 1.0, f"处理失败: {exc}")

    def _progress(self, job: JobRecord, progress: float, message: str) -> None:
        with self._lock:
            job.progress = max(0.0, min(0.99, progress))
            job.updated_at = iso_now()
            job.events.append(JobEvent(at=job.updated_at, type="progress", message=message, progress=job.progress))
            self._save_job(job)

    def _set_status(self, job: JobRecord, status: str, progress: float, message: str) -> None:
        with self._lock:
            job.status = status
            job.progress = progress
            job.updated_at = iso_now()
            job.events.append(JobEvent(at=job.updated_at, type=status, message=message, progress=progress))
            self._save_job(job)

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
            self._save_job(job)

    def _output_dir_for(self, source_name: str, job_id: str) -> Path:
        stem = safe_path_stem(source_name, fallback="transcript")
        return self.output_root / f"{stem}_{short_timestamp()}_{job_id[:8]}"

    def _load_history(self) -> None:
        with self._lock:
            if self._jobs:
                return
            for output_dir in sorted(self.output_root.iterdir() if self.output_root.exists() else []):
                if not output_dir.is_dir():
                    continue
                job = self._load_job_from_dir(output_dir)
                if job is not None and job.id not in self._jobs:
                    self._jobs[job.id] = job

    def _load_job_from_dir(self, output_dir: Path) -> JobRecord | None:
        metadata_path = output_dir / JOB_METADATA_FILE
        if metadata_path.exists():
            try:
                payload = json.loads(metadata_path.read_text(encoding="utf-8"))
                return _job_from_payload(payload, output_dir)
            except Exception:
                return None
        return _legacy_job_from_output_dir(output_dir, self.input_dir)

    def _save_job(self, job: JobRecord) -> None:
        try:
            job.output_dir.mkdir(parents=True, exist_ok=True)
            payload = job.snapshot()
            payload["schema_version"] = 1
            (job.output_dir / JOB_METADATA_FILE).write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass


def parse_formats(value: str) -> set[str]:
    values = {item.strip().lower() for item in value.split(",") if item.strip()}
    allowed = {"txt", "srt", "vtt", "tsv", "csv", "json"}
    if "all" in values:
        return allowed
    return values & allowed or {"txt"}


def sanitize_filename(filename: str) -> str:
    name = Path(filename or "audio.mp3").name
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(". ")
    return name or f"audio_{short_timestamp()}.mp3"


def safe_path_stem(value: str, fallback: str = "audio", max_length: int = 80) -> str:
    text = str(value or "").strip()
    stem = Path(text).stem if text else ""
    stem = stem or text
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", stem)
    stem = re.sub(r"\s+", " ", stem).strip(". _")
    stem = stem[:max_length].rstrip(". _")
    return stem or fallback


def display_source_name(value: str, fallback_path: Path) -> str:
    suffix = fallback_path.suffix or Path(str(value or "")).suffix
    return f"{safe_path_stem(value, fallback=fallback_path.stem)}{suffix}"


def short_timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _job_from_payload(payload: dict, output_dir: Path) -> JobRecord:
    options = _options_from_payload(payload.get("options") or {})
    status = str(payload.get("status") or "completed")
    progress = float(payload.get("progress") or (1.0 if status in TERMINAL_STATUSES else 0.0))
    error = str(payload.get("error") or "")
    events = [
        JobEvent(
            at=str(item.get("at") or iso_now()),
            type=str(item.get("type") or "history"),
            message=str(item.get("message") or ""),
            progress=item.get("progress"),
        )
        for item in payload.get("events") or []
        if isinstance(item, dict)
    ]
    if status not in TERMINAL_STATUSES:
        status = "failed"
        progress = 1.0
        error = error or "服务重启后任务未继续执行。"
        events.append(JobEvent(at=iso_now(), type="failed", message=error, progress=1.0))
    return JobRecord(
        id=str(payload.get("id") or output_dir.name),
        source_name=str(payload.get("source_name") or output_dir.name),
        input_path=Path(str(payload.get("input_path") or "")),
        status=status,
        progress=progress,
        options=options,
        formats=set(payload.get("formats") or _outputs_from_dir(output_dir).keys()),
        created_at=str(payload.get("created_at") or iso_now()),
        updated_at=str(payload.get("updated_at") or iso_now()),
        output_dir=output_dir,
        outputs={key: Path(value) for key, value in (payload.get("outputs") or _outputs_from_dir(output_dir)).items()},
        segments=list(payload.get("segments") or []),
        duration_seconds=payload.get("duration_seconds"),
        error=error,
        hidden=bool(payload.get("hidden", False)),
        events=events,
    )


def _options_from_payload(raw: dict) -> EngineOptions:
    values: dict[str, Any] = {}
    option_fields = {item.name for item in fields(EngineOptions)}
    for key, value in raw.items():
        if key in option_fields and key != "work_dir":
            values[key] = value
    options = EngineOptions(**values)
    work_dir = raw.get("work_dir")
    if work_dir:
        options.work_dir = Path(str(work_dir))
    return options


def _legacy_job_from_output_dir(output_dir: Path, input_dir: Path) -> JobRecord | None:
    outputs = _outputs_from_dir(output_dir)
    if not outputs:
        return None
    payload = _read_legacy_result_json(outputs.get("json"))
    source_name = str(payload.get("source") or next(iter(outputs.values())).name)
    timestamp = datetime.fromtimestamp(output_dir.stat().st_mtime, timezone.utc).isoformat()
    options = EngineOptions(engine_id=str(payload.get("engine") or ""))
    hidden = bool(re.fullmatch(r"chunk_\d{4}\.wav", source_name))
    return JobRecord(
        id=output_dir.name,
        source_name=source_name,
        input_path=input_dir / source_name,
        status="completed",
        progress=1.0,
        options=options,
        formats=set(outputs.keys()),
        created_at=timestamp,
        updated_at=timestamp,
        output_dir=output_dir,
        outputs=outputs,
        segments=list(payload.get("segments") or []),
        duration_seconds=payload.get("duration_seconds"),
        hidden=hidden,
        events=[JobEvent(at=timestamp, type="history", message="从已有输出恢复历史记录", progress=1.0)],
    )


def _outputs_from_dir(output_dir: Path) -> dict[str, Path]:
    outputs: dict[str, Path] = {}
    for path in output_dir.iterdir() if output_dir.exists() else []:
        if not path.is_file() or path.name == JOB_METADATA_FILE:
            continue
        suffix = path.suffix.lower().lstrip(".")
        if suffix in OUTPUT_EXTS:
            outputs[suffix] = path
    return outputs


def _read_legacy_result_json(path: Path | None) -> dict:
    if path is None or not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


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
