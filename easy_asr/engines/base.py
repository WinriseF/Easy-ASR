from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from easy_asr.terminology import TerminologyLibrary


ProgressCallback = Callable[[float, str], None]


@dataclass
class Segment:
    index: int
    start: float | None
    end: float | None
    text: str
    raw: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "start": self.start,
            "end": self.end,
            "text": self.text,
            "raw": self.raw,
        }


@dataclass
class TranscriptionResult:
    engine_id: str
    language: str
    duration_seconds: float | None
    segments: list[Segment]


@dataclass
class EngineDescriptor:
    id: str
    label: str
    description: str
    available: bool
    priority: int
    capabilities: list[str]
    install_hint: str = ""
    default_model: str = ""


@dataclass
class EngineOptions:
    engine_id: str = "funasr-sensevoice"
    model_name: str = ""
    language: str = "zh"
    chunk_seconds: int = 600
    batch_size_s: int = 60
    merge_length_s: int = 15
    cpu_threads: int = 4
    compute_type: str = "int8"
    whisper_preset: str = "balanced"
    apply_terminology: bool = True
    transcript_mode: str = "whole"
    work_dir: Path | None = None


class ASREngine(Protocol):
    id: str

    def transcribe(
        self,
        input_path: Path,
        options: EngineOptions,
        terminology: TerminologyLibrary,
        progress: ProgressCallback,
    ) -> TranscriptionResult:
        ...
