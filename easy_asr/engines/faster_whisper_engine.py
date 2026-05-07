from __future__ import annotations

import importlib.util
from pathlib import Path
from threading import Lock

from easy_asr.audio import probe_duration
from easy_asr.engines.base import EngineDescriptor, EngineOptions, Segment, TranscriptionResult
from easy_asr.segmentation import sentence_segments_from_words
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
            model_choices=[
                {"name": "tiny", "label": "tiny（最快）"},
                {"name": "base", "label": "base"},
                {"name": "small", "label": "small（推荐）"},
                {"name": "medium", "label": "medium"},
                {"name": "large-v3", "label": "large-v3（质量高）"},
            ],
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
        transcript_mode = _transcript_mode(options.transcript_mode)
        progress(0.08, f"正在启动 faster-whisper CPU 转写（beam_size={beam_size}）")
        raw_segments, info = model.transcribe(
            str(input_path),
            language=None if options.language == "auto" else options.language,
            vad_filter=True,
            initial_prompt=initial_prompt or None,
            beam_size=beam_size,
            word_timestamps=transcript_mode == "sentence",
        )

        segments: list[Segment] = []
        whole_texts: list[str] = []
        whole_start: float | None = None
        whole_end: float | None = None
        duration = info.duration or probe_duration(input_path) or 0
        for item in raw_segments:
            text = item.text.strip()
            raw = {"avg_logprob": item.avg_logprob, "no_speech_prob": item.no_speech_prob}
            start = float(item.start)
            end = float(item.end)
            if text:
                whole_texts.append(text)
                whole_start = start if whole_start is None else min(whole_start, start)
                whole_end = end if whole_end is None else max(whole_end, end)

            if transcript_mode == "whole":
                if duration:
                    progress(min(0.92, end / duration * 0.84 + 0.08), "正在生成整体转写文本")
                continue

            if transcript_mode == "chunk":
                if options.apply_terminology and text:
                    text = terminology.apply(text)
                if text:
                    segments.append(
                        Segment(
                            index=len(segments) + 1,
                            start=start,
                            end=end,
                            text=text,
                            raw={**raw, "timing": "chunk"},
                        )
                    )
                if duration:
                    progress(min(0.92, end / duration * 0.84 + 0.08), "正在生成切片文本")
                continue

            sentence_segments = sentence_segments_from_words(
                index_start=len(segments) + 1,
                segment_start=start,
                segment_end=end,
                words=list(getattr(item, "words", None) or []),
                fallback_text=text,
                raw=raw,
            )
            for segment in sentence_segments:
                if options.apply_terminology and segment.text:
                    segment.text = terminology.apply(segment.text)
                segment.index = len(segments) + 1
                segments.append(segment)
            if duration:
                progress(min(0.92, end / duration * 0.84 + 0.08), "正在生成带时间戳片段")

        if transcript_mode == "whole":
            text = "\n".join(whole_texts).strip()
            if options.apply_terminology and text:
                text = terminology.apply(text)
            if text:
                segments.append(
                    Segment(
                        index=1,
                        start=whole_start if whole_start is not None else 0.0,
                        end=whole_end,
                        text=text,
                        raw={"timing": "whole"},
                    )
                )

        return TranscriptionResult(
            engine_id=self.id,
            language=info.language or options.language,
            duration_seconds=info.duration or probe_duration(input_path),
            segments=segments,
        )

    def preload(self, options: EngineOptions) -> None:
        from faster_whisper import WhisperModel

        model_name = options.model_name or "small"
        self._load_model(WhisperModel, model_name, options)

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


def _transcript_mode(value: str) -> str:
    return value if value in {"whole", "chunk", "sentence"} else "whole"


def _beam_size_for_preset(value: str) -> int:
    return {
        "fast": 1,
        "balanced": 3,
        "quality": 5,
    }.get(value, 3)
