from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from threading import Lock

from easy_asr.debug_runtime import flush_logging, get_logger, log_debug, shorten


@dataclass(frozen=True)
class AudioChunk:
    path: Path
    start: float
    end: float | None


_DURATION_CACHE: dict[tuple[str, int, int], float | None] = {}
_DURATION_LOCK = Lock()
LOGGER = get_logger(__name__)


def ffmpeg_exe() -> str:
    configured = os.environ.get("EASY_ASR_FFMPEG_EXE")
    if configured and Path(configured).exists():
        log_debug(LOGGER, "ffmpeg_resolved", source="env", path=configured)
        return configured

    found = shutil.which("ffmpeg")
    if found:
        log_debug(LOGGER, "ffmpeg_resolved", source="path", path=found)
        return found

    raise RuntimeError("未找到 ffmpeg，请确认打包目录中包含 bin/ffmpeg.exe。")


def ffprobe_exe() -> str:
    configured = os.environ.get("EASY_ASR_FFPROBE_EXE")
    if configured and Path(configured).exists():
        log_debug(LOGGER, "ffprobe_resolved", source="env", path=configured)
        return configured

    found = shutil.which("ffprobe")
    if found:
        log_debug(LOGGER, "ffprobe_resolved", source="path", path=found)
        return found

    raise RuntimeError("未找到 ffprobe，请确认打包目录中包含 bin/ffprobe.exe。")


def require_ffmpeg() -> None:
    ffmpeg_exe()


def require_ffprobe() -> None:
    ffprobe_exe()


def probe_duration(path: Path) -> float | None:
    try:
        stat = path.stat()
        cache_key = (str(path.resolve()), stat.st_size, stat.st_mtime_ns)
    except OSError:
        return None

    with _DURATION_LOCK:
        if cache_key in _DURATION_CACHE:
            return _DURATION_CACHE[cache_key]

    duration = _probe_duration_uncached(path)
    with _DURATION_LOCK:
        _DURATION_CACHE[cache_key] = duration
    return duration


def _probe_duration_uncached(path: Path) -> float | None:
    try:
        ffprobe_path = ffprobe_exe()
    except RuntimeError:
        return None

    cmd = [
        ffprobe_path,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    try:
        completed = subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.CalledProcessError):
        LOGGER.exception("probe_duration_subprocess_failed")
        flush_logging()
        return None
    log_debug(
        LOGGER,
        "probe_duration_completed",
        path=path,
        returncode=completed.returncode,
        stdout=shorten(completed.stdout.strip(), 200),
        stderr=shorten(completed.stderr.strip(), 200),
    )
    flush_logging()
    value = completed.stdout.strip()
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def split_audio_to_chunks(
    audio_path: Path,
    chunk_dir: Path,
    chunk_seconds: int,
) -> list[AudioChunk]:
    require_ffmpeg()
    chunk_dir.mkdir(parents=True, exist_ok=True)

    existing_chunks = sorted(chunk_dir.glob("chunk_*.wav"))
    if not existing_chunks:
        output_pattern = str(chunk_dir / "chunk_%04d.wav")
        cmd = [
            ffmpeg_exe(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(audio_path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            "-f",
            "segment",
            "-segment_time",
            str(chunk_seconds),
            "-reset_timestamps",
            "1",
            output_pattern,
        ]
        log_debug(
            LOGGER,
            "split_audio_to_chunks_running",
            audio_path=audio_path,
            chunk_dir=chunk_dir,
            chunk_seconds=chunk_seconds,
            cmd=cmd,
        )
        flush_logging()
        subprocess.run(cmd, check=True)
        existing_chunks = sorted(chunk_dir.glob("chunk_*.wav"))

    if not existing_chunks:
        raise RuntimeError(f"音频切片失败: {audio_path}")

    total_duration = probe_duration(audio_path)
    chunks: list[AudioChunk] = []
    for index, path in enumerate(existing_chunks):
        start = float(index * chunk_seconds)
        if total_duration is None:
            chunk_duration = probe_duration(path)
            end = start + chunk_duration if chunk_duration is not None else None
        else:
            end = min(total_duration, start + chunk_seconds)
        chunks.append(AudioChunk(path=path, start=start, end=end))
    return chunks
