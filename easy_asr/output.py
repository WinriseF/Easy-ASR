from __future__ import annotations

import json
from pathlib import Path

from easy_asr.engines.base import Segment, TranscriptionResult


def write_outputs(
    output_dir: Path,
    source_name: str,
    result: TranscriptionResult,
    formats: set[str],
) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(source_name).stem
    written: dict[str, Path] = {}

    if "txt" in formats:
        path = output_dir / f"{stem}.txt"
        path.write_text(_txt(result.segments), encoding="utf-8")
        written["txt"] = path

    if "srt" in formats:
        path = output_dir / f"{stem}.srt"
        path.write_text(_srt(result.segments), encoding="utf-8")
        written["srt"] = path

    if "json" in formats:
        path = output_dir / f"{stem}.json"
        payload = {
            "source": source_name,
            "engine": result.engine_id,
            "language": result.language,
            "duration_seconds": result.duration_seconds,
            "segments": [segment.to_dict() for segment in result.segments],
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        written["json"] = path

    return written


def _txt(segments: list[Segment]) -> str:
    return "\n\n".join(segment.text for segment in segments if segment.text).strip() + "\n"


def _srt(segments: list[Segment]) -> str:
    blocks: list[str] = []
    for segment in segments:
        start = _timestamp(segment.start or 0)
        end = _timestamp(segment.end if segment.end is not None else (segment.start or 0) + 1)
        blocks.append(f"{segment.index}\n{start} --> {end}\n{segment.text}")
    return "\n\n".join(blocks).strip() + "\n"


def _timestamp(seconds: float) -> str:
    milliseconds = int(round(seconds * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"

