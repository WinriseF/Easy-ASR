from __future__ import annotations

import importlib.util
from pathlib import Path
from threading import Lock

from easy_asr.audio import probe_duration
from easy_asr.engines.base import EngineDescriptor, EngineOptions, Segment, TranscriptionResult
from easy_asr.terminology import TerminologyLibrary


_MODEL_CACHE: dict[str, object] = {}
_MODEL_LOCK = Lock()


class FasterWhisperEngine:
    id = "faster-whisper"

    def __init__(self, base_dir: Path):
        self.base_dir = base_dir

    @staticmethod
    def descriptor() -> EngineDescriptor:
        available = importlib.util.find_spec("faster_whisper") is not None
        return EngineDescriptor(
            id=FasterWhisperEngine.id,
            label="faster-whisper",
            description="Whisper CTranslate2 推理；CPU int8 友好；可用 initial_prompt 注入术语库。",
            available=available,
            priority=20,
            capabilities=["cpu", "int8", "timestamps", "vad", "terminology-prompt", "quality-presets"],
            install_hint="安装 requirements-asr-faster-whisper.txt 后可用。",
            default_model="small",
        )

    def transcribe(
        self,
        input_path: Path,
        options: EngineOptions,
        terminology: TerminologyLibrary,
        progress,
    ) -> TranscriptionResult:
        from faster_whisper import WhisperModel

        model_name = options.model_name or "small"
        model = self._load_model(WhisperModel, model_name, options)
        initial_prompt = terminology.prompt_text() if options.apply_terminology else None
        beam_size = _beam_size_for_preset(options.whisper_preset)
        progress(0.08, f"正在启动 faster-whisper CPU 转写（beam_size={beam_size}）")
        raw_segments, info = model.transcribe(
            str(input_path),
            language=None if options.language == "auto" else options.language,
            vad_filter=True,
            initial_prompt=initial_prompt or None,
            beam_size=beam_size,
        )

        segments: list[Segment] = []
        for item in raw_segments:
            text = item.text.strip()
            if options.apply_terminology and text:
                text = terminology.apply(text)
            segments.append(
                Segment(
                    index=len(segments) + 1,
                    start=float(item.start),
                    end=float(item.end),
                    text=text,
                    raw={"avg_logprob": item.avg_logprob, "no_speech_prob": item.no_speech_prob},
                )
            )
            duration = info.duration or probe_duration(input_path) or 0
            if duration:
                progress(min(0.92, item.end / duration * 0.84 + 0.08), "正在生成带时间戳片段")

        return TranscriptionResult(
            engine_id=self.id,
            language=info.language or options.language,
            duration_seconds=info.duration or probe_duration(input_path),
            segments=segments,
        )

    def _load_model(self, model_cls, model_name: str, options: EngineOptions):
        cache_key = f"{self.id}:{model_name}:cpu:{options.compute_type}:{options.cpu_threads}"
        with _MODEL_LOCK:
            if cache_key not in _MODEL_CACHE:
                _MODEL_CACHE[cache_key] = model_cls(
                    model_name,
                    device="cpu",
                    compute_type=options.compute_type or "int8",
                    cpu_threads=max(1, int(options.cpu_threads or 1)),
                    download_root=str(self.base_dir / "models" / "faster-whisper"),
                )
            return _MODEL_CACHE[cache_key]


def _beam_size_for_preset(value: str) -> int:
    return {
        "fast": 1,
        "balanced": 3,
        "quality": 5,
    }.get(value, 3)
