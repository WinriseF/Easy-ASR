from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from threading import Lock

from easy_asr.audio import probe_duration, split_audio_to_chunks
from easy_asr.engines.base import EngineDescriptor, EngineOptions, Segment, TranscriptionResult
from easy_asr.terminology import TerminologyLibrary


_MODEL_CACHE: dict[str, object] = {}
_MODEL_LOCK = Lock()


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
        total = max(len(chunks), 1)
        for index, chunk in enumerate(chunks, start=1):
            progress(0.08 + (index - 1) / total * 0.84, f"正在转写第 {index}/{total} 段")
            result = model.generate(
                input=str(chunk.path),
                cache={},
                language=_funasr_language(options.language),
                use_itn=True,
                batch_size_s=options.batch_size_s,
                merge_vad=True,
                merge_length_s=options.merge_length_s,
            )
            texts: list[str] = []
            raw_items: list[dict] = []
            for item in result:
                raw_items.append(item)
                text = str(item.get("text", ""))
                text = rich_transcription_postprocess(text).strip()
                if text:
                    texts.append(text)
            text = "\n".join(texts).strip()
            if options.apply_terminology and text:
                text = terminology.apply(text)
            if text:
                segments.append(
                    Segment(
                        index=len(segments) + 1,
                        start=chunk.start,
                        end=chunk.end,
                        text=text,
                        raw={"items": raw_items, "chunk": str(chunk.path)},
                    )
                )

        progress(0.94, "正在整理转写结果")
        return TranscriptionResult(
            engine_id=self.id,
            language=options.language,
            duration_seconds=probe_duration(input_path),
            segments=segments,
        )

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

