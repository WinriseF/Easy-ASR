from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from threading import Lock


@dataclass(frozen=True)
class AudioChunk:
    path: Path
    start: float
    end: float | None


_DURATION_CACHE: dict[tuple[str, int, int], float | None] = {}
_DURATION_LOCK = Lock()


def require_ffmpeg() -> None:
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("未找到 ffmpeg，请先安装 ffmpeg 并加入 PATH。")


def require_ffprobe() -> None:
    if shutil.which("ffprobe") is None:
        raise RuntimeError("未找到 ffprobe，请先安装 ffmpeg 并加入 PATH。")


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
    if shutil.which("ffprobe") is None:
        return None

    cmd = [
        "ffprobe",
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
        return None
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
            "ffmpeg",
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
