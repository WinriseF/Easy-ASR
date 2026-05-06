from __future__ import annotations

import sys
import threading
import uuid
import wave
from array import array
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from easy_asr.engines.base import EngineOptions
from easy_asr.jobs import JobManager


CAPTURE_EXT = ".wav"
CAPTURE_TERMINAL_STATUSES = {"completed", "failed"}
INSTALL_HINT = "请先安装 requirements-capture-windows.txt，然后重启本地服务。"
LIVE_CHUNK_SECONDS = 20


@dataclass
class CaptureEvent:
    at: str
    type: str
    message: str
    progress: float | None = None


@dataclass
class CaptureChunkJob:
    job_id: str
    path: Path
    offset_seconds: float
    duration_seconds: float
    status: str = "queued"
    collected: bool = False

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "path": str(self.path),
            "offset_seconds": self.offset_seconds,
            "duration_seconds": self.duration_seconds,
            "status": self.status,
            "collected": self.collected,
        }


@dataclass
class CaptureRecord:
    id: str
    status: str
    created_at: str
    updated_at: str
    wav_path: Path
    device_index: int | None = None
    device_name: str = ""
    sample_rate: int = 0
    channels: int = 0
    duration_seconds: float = 0.0
    frames_recorded: int = 0
    bytes_recorded: int = 0
    level: float = 0.0
    error: str = ""
    job_id: str = ""
    live_chunk_seconds: int = LIVE_CHUNK_SECONDS
    chunks: list[CaptureChunkJob] = field(default_factory=list)
    live_segments: list[dict] = field(default_factory=list)
    events: list[CaptureEvent] = field(default_factory=list)

    def snapshot(self) -> dict:
        data = asdict(self)
        data["wav_path"] = str(self.wav_path)
        data["chunks"] = [chunk.to_dict() for chunk in self.chunks]
        return data

    def summary(self) -> dict:
        return {
            "id": self.id,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "wav_path": str(self.wav_path),
            "device_name": self.device_name,
            "duration_seconds": self.duration_seconds,
            "level": self.level,
            "error": self.error,
            "job_id": self.job_id,
            "live_segments": len(self.live_segments),
        }


class PlaybackCaptureManager:
    def __init__(self, base_dir: Path, job_manager: JobManager):
        self.base_dir = base_dir
        self.job_manager = job_manager
        self.capture_dir = base_dir / "input" / "captures"
        self._sessions: dict[str, CaptureRecord] = {}
        self._stop_events: dict[str, threading.Event] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._collector_threads: dict[str, threading.Thread] = {}
        self._options: dict[str, EngineOptions] = {}
        self._formats: dict[str, set[str]] = {}
        self._lock = threading.RLock()

    def ensure_dirs(self) -> None:
        self.capture_dir.mkdir(parents=True, exist_ok=True)

    def devices_payload(self) -> dict:
        try:
            devices = self.list_devices()
            return {"available": True, "install_hint": "", "devices": devices}
        except Exception as exc:
            return {"available": False, "install_hint": str(exc), "devices": []}

    def list_devices(self) -> list[dict]:
        pyaudio = _load_pyaudio()
        devices: list[dict] = []
        audio = pyaudio.PyAudio()
        try:
            default_index = _default_loopback_index(audio)
            try:
                loopback_devices = audio.get_loopback_device_info_generator()
            except AttributeError as exc:
                raise RuntimeError("当前 PyAudioWPatch 不支持 WASAPI loopback 设备枚举。") from exc
            for device in loopback_devices:
                devices.append(_device_payload(device, is_default=device.get("index") == default_index))
        finally:
            audio.terminate()
        return sorted(devices, key=lambda item: (not item["is_default"], item["name"]))

    def list_sessions(self) -> list[dict]:
        with self._lock:
            return [
                session.summary()
                for session in sorted(self._sessions.values(), key=lambda item: item.created_at, reverse=True)
            ]

    def get_session(self, session_id: str) -> CaptureRecord | None:
        with self._lock:
            return self._sessions.get(session_id)

    def start(
        self,
        options: EngineOptions,
        formats: set[str],
        device_index: int | None = None,
    ) -> CaptureRecord:
        with self._lock:
            active = [item for item in self._sessions.values() if item.status in {"starting", "recording", "stopping"}]
            if active:
                raise RuntimeError("已有系统音频采集正在运行，请先停止当前采集。")

        session_id = uuid.uuid4().hex[:12]
        now = iso_now()
        record = CaptureRecord(
            id=session_id,
            status="starting",
            created_at=now,
            updated_at=now,
            wav_path=self.capture_dir / f"capture_{session_id}{CAPTURE_EXT}",
            device_index=device_index,
        )
        record.events.append(CaptureEvent(at=now, type="starting", message="正在打开系统播放采集", progress=0))
        stop_event = threading.Event()
        with self._lock:
            self._sessions[session_id] = record
            self._stop_events[session_id] = stop_event
            self._options[session_id] = options
            self._formats[session_id] = formats or {"txt"}

        thread = threading.Thread(target=self._record, args=(session_id,), name=f"capture-{session_id}", daemon=True)
        collector_thread = threading.Thread(
            target=self._collect_live_jobs,
            args=(session_id,),
            name=f"capture-collector-{session_id}",
            daemon=True,
        )
        with self._lock:
            self._threads[session_id] = thread
            self._collector_threads[session_id] = collector_thread
        thread.start()
        collector_thread.start()
        return record

    def stop(self, session_id: str) -> CaptureRecord:
        record = self.get_session(session_id)
        if record is None:
            raise KeyError(session_id)
        if record.status == "failed":
            return record
        if record.status in {"transcribing", "completed"}:
            return record

        with self._lock:
            stop_event = self._stop_events.get(session_id)
            thread = self._threads.get(session_id)
            record.status = "stopping"
            record.updated_at = iso_now()
            record.events.append(
                CaptureEvent(at=record.updated_at, type="stopping", message="正在停止系统音频采集", progress=None)
            )

        if stop_event is not None:
            stop_event.set()
        if thread is not None:
            thread.join(timeout=10)
            if thread.is_alive():
                raise TimeoutError("停止采集超时，请稍后刷新状态。")

        record = self.get_session(session_id)
        if record is None:
            raise KeyError(session_id)
        return record

    def _record(self, session_id: str) -> None:
        record = self.get_session(session_id)
        stop_event = self._stop_events.get(session_id)
        if record is None or stop_event is None:
            return

        try:
            pyaudio = _load_pyaudio()
            audio = pyaudio.PyAudio()
            try:
                device = _resolve_loopback_device(audio, record.device_index)
                data_format = pyaudio.paInt16
                sample_width = audio.get_sample_size(data_format)
                sample_rate = int(float(device.get("defaultSampleRate") or 48000))
                channels = max(1, int(device.get("maxInputChannels") or 2))
                frames_per_buffer = 1024
                live_chunk_frames = max(sample_rate, sample_rate * record.live_chunk_seconds)
                chunk_dir = self.capture_dir / record.id
                chunk_dir.mkdir(parents=True, exist_ok=True)
                chunk_index = 0
                chunk_start_frames = 0
                chunk_frames = 0
                chunk_file = None
                chunk_path = None

                self._mark_recording(record, device, sample_rate, channels)
                with wave.open(str(record.wav_path), "wb") as wave_file:
                    wave_file.setnchannels(channels)
                    wave_file.setsampwidth(sample_width)
                    wave_file.setframerate(sample_rate)
                    stream = audio.open(
                        format=data_format,
                        channels=channels,
                        rate=sample_rate,
                        input=True,
                        input_device_index=int(device["index"]),
                        frames_per_buffer=frames_per_buffer,
                    )
                    try:
                        chunk_file, chunk_path = _open_chunk_file(
                            chunk_dir,
                            chunk_index,
                            channels,
                            sample_width,
                            sample_rate,
                        )
                        while not stop_event.is_set():
                            data = stream.read(frames_per_buffer, exception_on_overflow=False)
                            if not data:
                                continue
                            wave_file.writeframes(data)
                            chunk_file.writeframes(data)
                            frame_count = len(data) // max(1, channels * sample_width)
                            chunk_frames += frame_count
                            self._update_recording(record, frame_count, len(data), sample_rate, _pcm_level(data))
                            if chunk_frames >= live_chunk_frames:
                                chunk_file.close()
                                self._submit_live_chunk(record, chunk_path, chunk_start_frames / sample_rate, chunk_frames / sample_rate)
                                chunk_index += 1
                                chunk_start_frames += chunk_frames
                                chunk_frames = 0
                                chunk_file, chunk_path = _open_chunk_file(
                                    chunk_dir,
                                    chunk_index,
                                    channels,
                                    sample_width,
                                    sample_rate,
                                )
                    finally:
                        if chunk_file is not None:
                            chunk_file.close()
                            if chunk_frames > 0 and chunk_path is not None:
                                self._submit_live_chunk(
                                    record,
                                    chunk_path,
                                    chunk_start_frames / sample_rate,
                                    chunk_frames / sample_rate,
                                )
                        if stream.is_active():
                            stream.stop_stream()
                        stream.close()
                self._mark_transcribing_or_completed(record)
            finally:
                audio.terminate()
        except Exception as exc:
            self._mark_failed(record, exc)

    def _submit_live_chunk(
        self,
        record: CaptureRecord,
        chunk_path: Path,
        offset_seconds: float,
        duration_seconds: float,
    ) -> None:
        if not chunk_path.exists() or chunk_path.stat().st_size == 0:
            return
        options = self._options.get(record.id) or EngineOptions()
        formats = {"json"}
        job = self.job_manager.submit(chunk_path, options, formats, hidden=True)
        with self._lock:
            record.job_id = job.id
            record.chunks.append(
                CaptureChunkJob(
                    job_id=job.id,
                    path=chunk_path,
                    offset_seconds=offset_seconds,
                    duration_seconds=duration_seconds,
                )
            )
            record.updated_at = iso_now()
            record.events.append(
                CaptureEvent(
                    at=record.updated_at,
                    type="chunk_submitted",
                    message=f"实时切片已提交转写: {len(record.chunks)}",
                    progress=None,
                )
            )

    def _collect_live_jobs(self, session_id: str) -> None:
        while True:
            record = self.get_session(session_id)
            if record is None:
                return
            all_done = record.status in CAPTURE_TERMINAL_STATUSES
            any_pending = False
            with self._lock:
                chunks = list(record.chunks)
            for chunk in chunks:
                if chunk.collected:
                    continue
                job = self.job_manager.get_job(chunk.job_id)
                if job is None:
                    any_pending = True
                    continue
                chunk.status = job.status
                if job.status == "completed":
                    self._collect_chunk(record, chunk, job.segments)
                elif job.status == "failed":
                    chunk.collected = True
                    with self._lock:
                        record.updated_at = iso_now()
                        record.events.append(
                            CaptureEvent(
                                at=record.updated_at,
                                type="chunk_failed",
                                message=f"实时切片转写失败: {job.error}",
                                progress=None,
                            )
                        )
                else:
                    any_pending = True

            record = self.get_session(session_id)
            if record is None:
                return
            if record.status == "transcribing" and not any_pending:
                with self._lock:
                    if all(chunk.collected for chunk in record.chunks):
                        record.status = "completed"
                        record.updated_at = iso_now()
                        record.events.append(
                            CaptureEvent(at=record.updated_at, type="completed", message="实时转写完成", progress=1)
                        )
                return
            if all_done:
                return
            time_sleep(1)

    def _collect_chunk(self, record: CaptureRecord, chunk: CaptureChunkJob, segments: list[dict]) -> None:
        collected: list[dict] = []
        for segment in segments:
            item = dict(segment)
            start = item.get("start")
            end = item.get("end")
            item["start"] = None if start is None else float(start) + chunk.offset_seconds
            item["end"] = None if end is None else float(end) + chunk.offset_seconds
            item["index"] = len(record.live_segments) + len(collected) + 1
            collected.append(item)
        with self._lock:
            record.live_segments.extend(collected)
            chunk.collected = True
            chunk.status = "completed"
            record.updated_at = iso_now()
            if collected:
                record.events.append(
                    CaptureEvent(
                        at=record.updated_at,
                        type="live_text",
                        message=f"新增实时转写片段: {len(collected)}",
                        progress=None,
                    )
                )

    def _mark_recording(self, record: CaptureRecord, device: dict, sample_rate: int, channels: int) -> None:
        with self._lock:
            record.status = "recording"
            record.device_index = int(device["index"])
            record.device_name = str(device.get("name") or f"Device {device['index']}")
            record.sample_rate = sample_rate
            record.channels = channels
            record.updated_at = iso_now()
            record.events.append(
                CaptureEvent(at=record.updated_at, type="recording", message=f"正在采集: {record.device_name}", progress=0)
            )

    def _update_recording(
        self,
        record: CaptureRecord,
        frame_count: int,
        byte_count: int,
        sample_rate: int,
        level: float,
    ) -> None:
        with self._lock:
            record.frames_recorded += frame_count
            record.bytes_recorded += byte_count
            record.duration_seconds = record.frames_recorded / sample_rate if sample_rate else 0.0
            record.level = level
            record.updated_at = iso_now()

    def _mark_transcribing_or_completed(self, record: CaptureRecord) -> None:
        with self._lock:
            record.status = "transcribing" if record.chunks else "completed"
            record.level = 0.0
            record.updated_at = iso_now()
            record.events.append(
                CaptureEvent(at=record.updated_at, type=record.status, message="系统音频采集已停止，正在整理实时转写", progress=1)
            )

    def _mark_failed(self, record: CaptureRecord, exc: Exception) -> None:
        with self._lock:
            record.status = "failed"
            record.error = str(exc)
            record.level = 0.0
            record.updated_at = iso_now()
            record.events.append(
                CaptureEvent(at=record.updated_at, type="failed", message=f"系统音频采集失败: {exc}", progress=1)
            )


def _load_pyaudio():
    try:
        import pyaudiowpatch as pyaudio
    except ImportError as exc:
        raise RuntimeError(INSTALL_HINT) from exc
    return pyaudio


def _open_chunk_file(
    chunk_dir: Path,
    chunk_index: int,
    channels: int,
    sample_width: int,
    sample_rate: int,
):
    chunk_path = chunk_dir / f"chunk_{chunk_index:04d}.wav"
    chunk_file = wave.open(str(chunk_path), "wb")
    chunk_file.setnchannels(channels)
    chunk_file.setsampwidth(sample_width)
    chunk_file.setframerate(sample_rate)
    return chunk_file, chunk_path


def time_sleep(seconds: float) -> None:
    threading.Event().wait(seconds)


def _default_loopback_index(audio) -> int | None:
    try:
        return int(audio.get_default_wasapi_loopback()["index"])
    except Exception:
        return None


def _resolve_loopback_device(audio, device_index: int | None) -> dict:
    if device_index is None:
        return audio.get_default_wasapi_loopback()

    device = audio.get_device_info_by_index(device_index)
    if device.get("isLoopbackDevice"):
        return device
    return audio.get_wasapi_loopback_analogue_by_index(device_index)


def _device_payload(device: dict[str, Any], is_default: bool = False) -> dict:
    return {
        "index": int(device["index"]),
        "name": str(device.get("name") or f"Device {device['index']}"),
        "sample_rate": int(float(device.get("defaultSampleRate") or 0)),
        "channels": int(device.get("maxInputChannels") or 0),
        "is_default": bool(is_default),
    }


def _pcm_level(data: bytes) -> float:
    if not data:
        return 0.0
    samples = array("h")
    usable = len(data) - (len(data) % samples.itemsize)
    samples.frombytes(data[:usable])
    if sys.byteorder == "big":
        samples.byteswap()
    if not samples:
        return 0.0
    step = max(1, len(samples) // 1200)
    peak = max(abs(value) for value in samples[::step])
    return min(1.0, peak / 32768.0)


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()
