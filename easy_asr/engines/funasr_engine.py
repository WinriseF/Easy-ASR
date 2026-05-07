from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from threading import Lock

from easy_asr.audio import probe_duration, split_audio_to_chunks
from easy_asr.engines.base import EngineDescriptor, EngineOptions, Segment, TranscriptionResult
from easy_asr.segmentation import split_text_segment
from easy_asr.terminology import TerminologyLibrary


_MODEL_CACHE: dict[str, object] = {}
_MODEL_LOCK = Lock()
_TIMESTAMP_UNSUPPORTED_MODELS: set[int] = set()


class FunASRSenseVoiceEngine:
    id = "funasr-sensevoice"

    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
        os.environ.setdefault("MODELSCOPE_CACHE", str(base_dir / "models" / "modelscope"))
        os.environ.setdefault("HF_HOME", str(base_dir / "models" / "hf"))
        os.environ.setdefault("TORCH_HOME", str(base_dir / "models" / "torch"))

    @staticmethod
    def descriptor() -> EngineDescriptor:
        available = importlib.util.find_spec("funasr") is not None
        return EngineDescriptor(
            id=FunASRSenseVoiceEngine.id,
            label="SenseVoiceSmall / FunASR",
            description="中文和多语种优先；CPU 可用；富文本、VAD、ITN 能力完整。当前术语库用于后处理增强。",
            available=available,
            priority=10,
            capabilities=["cpu", "vad", "itn", "rich-text", "terminology-postprocess"],
            install_hint="安装 requirements-asr-funasr.txt 后可用。",
            default_model="iic/SenseVoiceSmall",
            model_choices=[
                {"name": "iic/SenseVoiceSmall", "label": "SenseVoiceSmall（推荐）"},
                {"name": "iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch", "label": "Paraformer 中文 large"},
            ],
        )

    def transcribe(
        self,
        input_path: Path,
        options: EngineOptions,
        terminology: TerminologyLibrary,
        progress,
    ) -> TranscriptionResult:
        from funasr import AutoModel
        from funasr.utils.postprocess_utils import rich_transcription_postprocess

        model_name = options.model_name or "iic/SenseVoiceSmall"
        model = self._load_model(AutoModel, model_name)
        work_dir = options.work_dir or (self.base_dir / "chunks" / "_jobs" / input_path.stem)
        progress(0.04, "正在切分音频")
        chunks = split_audio_to_chunks(input_path, work_dir / "chunks", options.chunk_seconds)

        segments: list[Segment] = []
        whole_texts: list[str] = []
        whole_start: float | None = None
        whole_end: float | None = None
        transcript_mode = _transcript_mode(options.transcript_mode)
        total = max(len(chunks), 1)
        for index, chunk in enumerate(chunks, start=1):
            progress(0.08 + (index - 1) / total * 0.84, f"正在转写第 {index}/{total} 段")
            generate_kwargs = {
                "input": str(chunk.path),
                "cache": {},
                "language": _funasr_language(options.language),
                "use_itn": True,
                "batch_size_s": options.batch_size_s,
                "merge_vad": True,
                "merge_length_s": options.merge_length_s,
            }
            if transcript_mode == "sentence":
                result = _generate_with_optional_timestamps(model, **generate_kwargs)
            else:
                result = model.generate(**generate_kwargs)
            texts: list[str] = []
            raw_items: list[dict] = []
            for item in result:
                raw_items.append(item)
                text = str(item.get("text", ""))
                text = rich_transcription_postprocess(text).strip()
                if text:
                    texts.append(text)
            text = "\n".join(texts).strip()
            chunk_duration = probe_duration(chunk.path)
            chunk_end = chunk.end if chunk.end is not None else chunk.start + (chunk_duration or 1.0)

            if text:
                whole_texts.append(text)
                whole_start = chunk.start if whole_start is None else min(whole_start, chunk.start)
                whole_end = chunk_end if whole_end is None else max(whole_end, chunk_end)

            if transcript_mode == "whole":
                continue

            if options.apply_terminology and text:
                text = terminology.apply(text)
            if transcript_mode == "chunk":
                if text:
                    segments.append(
                        Segment(
                            index=len(segments) + 1,
                            start=chunk.start,
                            end=chunk_end,
                            text=text,
                            raw={"items": raw_items, "chunk": str(chunk.path), "timing": "chunk"},
                        )
                    )
                continue

            timestamp_segments = _segments_from_funasr_timestamps(
                index_start=len(segments) + 1,
                chunk_start=chunk.start,
                chunk_end=chunk_end,
                raw_items=raw_items,
                terminology=terminology if options.apply_terminology else None,
                postprocess=rich_transcription_postprocess,
            )
            if timestamp_segments:
                segments.extend(timestamp_segments)
            elif text:
                segments.extend(
                    split_text_segment(
                        index_start=len(segments) + 1,
                        start=chunk.start,
                        end=chunk_end,
                        text=text,
                        raw={"items": raw_items, "chunk": str(chunk.path)},
                    )
                )

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

        progress(0.94, "正在整理转写结果")
        return TranscriptionResult(
            engine_id=self.id,
            language=options.language,
            duration_seconds=probe_duration(input_path),
            segments=segments,
        )

    def preload(self, options: EngineOptions) -> None:
        from funasr import AutoModel

        model_name = options.model_name or "iic/SenseVoiceSmall"
        self._load_model(AutoModel, model_name)

    def _load_model(self, auto_model, model_name: str):
        cache_key = f"{self.id}:{model_name}:cpu"
        with _MODEL_LOCK:
            if cache_key not in _MODEL_CACHE:
                _MODEL_CACHE[cache_key] = auto_model(
                    model=model_name,
                    trust_remote_code=True,
                    vad_model="fsmn-vad",
                    vad_kwargs={"max_single_segment_time": 30000},
                    device="cpu",
                    disable_update=True,
                )
            return _MODEL_CACHE[cache_key]


def _funasr_language(language: str) -> str:
    if language in {"", "auto"}:
        return "auto"
    return language


def _transcript_mode(value: str) -> str:
    return value if value in {"whole", "chunk", "sentence"} else "whole"


def _generate_with_optional_timestamps(model, **kwargs):
    model_id = id(model)
    if model_id in _TIMESTAMP_UNSUPPORTED_MODELS:
        return model.generate(**kwargs)

    timestamp_kwargs = {
        **kwargs,
        "sentence_timestamp": True,
        "output_timestamp": True,
    }
    try:
        return model.generate(**timestamp_kwargs)
    except TypeError:
        _TIMESTAMP_UNSUPPORTED_MODELS.add(model_id)
        return model.generate(**kwargs)
    except UnboundLocalError as exc:
        if "punc_res" not in str(exc):
            raise
        _TIMESTAMP_UNSUPPORTED_MODELS.add(model_id)
        return model.generate(**kwargs)


def _segments_from_funasr_timestamps(
    index_start: int,
    chunk_start: float,
    chunk_end: float,
    raw_items: list[dict],
    terminology: TerminologyLibrary | None,
    postprocess,
) -> list[Segment]:
    segments: list[Segment] = []
    for item in raw_items:
        for sentence in _iter_sentence_info(item):
            text = postprocess(str(sentence.get("text") or sentence.get("sentence") or "")).strip()
            if not text:
                continue
            start, end = _sentence_bounds(sentence, chunk_start, chunk_end)
            if terminology is not None:
                text = terminology.apply(text)
            segments.append(
                Segment(
                    index=index_start + len(segments),
                    start=start,
                    end=end,
                    text=text,
                    raw={"item": item, "sentence": sentence, "timing": "funasr"},
                )
            )
    return segments


def _iter_sentence_info(item: dict) -> list[dict]:
    for key in ("sentence_info", "sentences", "sentence_timestamp"):
        value = item.get(key)
        if isinstance(value, list):
            return [entry for entry in value if isinstance(entry, dict)]
    return []


def _sentence_bounds(sentence: dict, chunk_start: float, chunk_end: float) -> tuple[float, float]:
    start = _time_value(sentence.get("start") or sentence.get("begin") or sentence.get("start_time"), chunk_start)
    end = _time_value(sentence.get("end") or sentence.get("stop") or sentence.get("end_time"), chunk_start)
    timestamp = sentence.get("timestamp")
    if isinstance(timestamp, list) and len(timestamp) >= 2:
        start = _time_value(timestamp[0], chunk_start)
        end = _time_value(timestamp[1], chunk_start)
    if start is None:
        start = chunk_start
    if end is None or end <= start:
        end = min(chunk_end, start + 1.0)
    return start, end


def _time_value(value, offset: float) -> float | None:
    try:
        if value is None:
            return None
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if numeric > 1000:
        numeric /= 1000.0
    return offset + numeric
