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

    if "vtt" in formats:
        path = output_dir / f"{stem}.vtt"
        path.write_text(_vtt(result.segments), encoding="utf-8")
        written["vtt"] = path

    if "tsv" in formats:
        path = output_dir / f"{stem}.tsv"
        path.write_text(_tsv(result.segments), encoding="utf-8")
        written["tsv"] = path

    if "csv" in formats:
        path = output_dir / f"{stem}.csv"
        path.write_text(_csv(result.segments), encoding="utf-8-sig")
        written["csv"] = path

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
        start, end = _segment_times(segment)
        blocks.append(f"{segment.index}\n{_srt_timestamp(start)} --> {_srt_timestamp(end)}\n{segment.text}")
    return "\n\n".join(blocks).strip() + "\n"


def _vtt(segments: list[Segment]) -> str:
    blocks = ["WEBVTT"]
    for segment in segments:
        start, end = _segment_times(segment)
        blocks.append(f"{segment.index}\n{_vtt_timestamp(start)} --> {_vtt_timestamp(end)}\n{segment.text}")
    return "\n\n".join(blocks).strip() + "\n"


def _tsv(segments: list[Segment]) -> str:
    rows = ["start_ms\tend_ms\ttext"]
    for segment in segments:
        start, end = _segment_times(segment)
        text = _single_line(segment.text)
        rows.append(f"{_milliseconds(start)}\t{_milliseconds(end)}\t{text}")
    return "\n".join(rows).strip() + "\n"


def _csv(segments: list[Segment]) -> str:
    rows = ['index,start_time,end_time,start_seconds,end_seconds,text']
    for segment in segments:
        start, end = _segment_times(segment)
        values = [
            str(segment.index),
            _vtt_timestamp(start),
            _vtt_timestamp(end),
            f"{start:.3f}",
            f"{end:.3f}",
            segment.text,
        ]
        rows.append(",".join(_csv_cell(value) for value in values))
    return "\n".join(rows).strip() + "\n"


def _segment_times(segment: Segment) -> tuple[float, float]:
    start = max(0.0, float(segment.start or 0.0))
    end = float(segment.end) if segment.end is not None else start + 1.0
    if end <= start:
        end = start + 1.0
    return start, end


def _srt_timestamp(seconds: float) -> str:
    milliseconds = int(round(seconds * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def _vtt_timestamp(seconds: float) -> str:
    milliseconds = int(round(seconds * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


def _milliseconds(seconds: float) -> int:
    return int(round(seconds * 1000))


def _single_line(value: str) -> str:
    return " ".join(str(value or "").split())


def _csv_cell(value: str) -> str:
    escaped = str(value or "").replace('"', '""')
    return f'"{escaped}"'
