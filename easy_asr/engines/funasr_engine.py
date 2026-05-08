from __future__ import annotations

import importlib.util
import os
import shutil
import sys
from pathlib import Path
from threading import Lock

from easy_asr.audio import probe_duration, split_audio_to_chunks
from easy_asr.debug_runtime import flush_logging, get_logger, log_debug, log_warning
from easy_asr.engines.base import EngineDescriptor, EngineOptions, Segment, TranscriptionResult
from easy_asr.segmentation import split_text_segment
from easy_asr.terminology import TerminologyLibrary


_MODEL_CACHE: dict[str, object] = {}
_MODEL_LOCK = Lock()
_TIMESTAMP_UNSUPPORTED_MODELS: set[int] = set()
LOGGER = get_logger(__name__)

_MODELSCOPE_MODEL_ALIASES = {
    "iic/SenseVoiceSmall": ("iic", "SenseVoiceSmall"),
    "iic/speech_fsmn_vad_zh-cn-16k-common-pytorch": ("iic", "speech_fsmn_vad_zh-cn-16k-common-pytorch"),
    "fsmn-vad": ("iic", "speech_fsmn_vad_zh-cn-16k-common-pytorch"),
    "iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch": (
        "iic",
        "speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
    ),
}

_FUNASR_HF_MODEL_ALIASES = {
    "iic/SenseVoiceSmall": "FunAudioLLM/SenseVoiceSmall",
}


class FunASRSenseVoiceEngine:
    id = "funasr-sensevoice"

    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
        os.environ.setdefault("MODELSCOPE_CACHE", str(base_dir / "models" / "modelscope"))
        os.environ.setdefault("HF_HOME", str(base_dir / "models" / "hf"))
        os.environ.setdefault("TORCH_HOME", str(base_dir / "models" / "torch"))
        self.bundle_dir = _bundle_base_dir(base_dir)
        self.model_roots = _model_roots(base_dir, self.bundle_dir)
        log_debug(
            LOGGER,
            "funasr_engine_initialized",
            base_dir=base_dir,
            bundle_dir=self.bundle_dir,
            modelscope_cache=os.environ.get("MODELSCOPE_CACHE", ""),
            hf_home=os.environ.get("HF_HOME", ""),
            torch_home=os.environ.get("TORCH_HOME", ""),
            ffmpeg_path=shutil.which("ffmpeg") or "",
            ffprobe_path=shutil.which("ffprobe") or "",
            model_roots=self.model_roots,
            frozen=bool(getattr(sys, "frozen", False)),
        )
        flush_logging()

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
        log_debug(
            LOGGER,
            "funasr_transcribe_start",
            input_path=input_path,
            model_name=options.model_name or "iic/SenseVoiceSmall",
            language=options.language,
            chunk_seconds=options.chunk_seconds,
            batch_size_s=options.batch_size_s,
            transcript_mode=options.transcript_mode,
            ffmpeg_path=shutil.which("ffmpeg") or "",
            frozen=bool(getattr(sys, "frozen", False)),
        )
        flush_logging()
        from funasr import AutoModel
        from funasr.utils.postprocess_utils import rich_transcription_postprocess
        log_debug(LOGGER, "funasr_import_completed", auto_model=str(AutoModel))
        flush_logging()

        model_name = options.model_name or "iic/SenseVoiceSmall"
        model = self._load_model(AutoModel, model_name)
        work_dir = options.work_dir or (self.base_dir / "chunks" / "_jobs" / input_path.stem)
        progress(0.04, "正在切分音频")
        chunks = split_audio_to_chunks(input_path, work_dir / "chunks", options.chunk_seconds)
        log_debug(LOGGER, "funasr_audio_chunks_ready", input_path=input_path, chunk_count=len(chunks), work_dir=work_dir)
        flush_logging()

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
                log_debug(
                    LOGGER,
                    "funasr_generate_start",
                    chunk_index=index,
                    total_chunks=total,
                    chunk_path=chunk.path,
                    timestamp_mode=True,
                )
                flush_logging()
                result = _generate_with_optional_timestamps(model, **generate_kwargs)
            else:
                log_debug(
                    LOGGER,
                    "funasr_generate_start",
                    chunk_index=index,
                    total_chunks=total,
                    chunk_path=chunk.path,
                    timestamp_mode=False,
                )
                flush_logging()
                result = model.generate(**generate_kwargs)
            log_debug(LOGGER, "funasr_generate_completed", chunk_index=index, total_chunks=total, chunk_path=chunk.path)
            flush_logging()
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
        log_debug(
            LOGGER,
            "funasr_preload_start",
            model_name=options.model_name or "iic/SenseVoiceSmall",
            ffmpeg_path=shutil.which("ffmpeg") or "",
            frozen=bool(getattr(sys, "frozen", False)),
        )
        flush_logging()
        from funasr import AutoModel
        log_debug(LOGGER, "funasr_import_completed", auto_model=str(AutoModel), preload=True)
        flush_logging()

        model_name = options.model_name or "iic/SenseVoiceSmall"
        self._load_model(AutoModel, model_name)

    def _load_model(self, auto_model, model_name: str):
        model_ref, model_path = self._resolve_model_reference(model_name)
        vad_ref, vad_path = self._resolve_model_reference("fsmn-vad")
        cache_key = f"{self.id}:{model_ref}:{vad_ref}:cpu"
        with _MODEL_LOCK:
            if cache_key not in _MODEL_CACHE:
                log_debug(
                    LOGGER,
                    "funasr_model_load_start",
                    requested_model=model_name,
                    resolved_model=model_ref,
                    resolved_model_path=model_path,
                    resolved_vad_model=vad_ref,
                    resolved_vad_path=vad_path,
                    cache_key=cache_key,
                    model_roots=self.model_roots,
                    ffmpeg_path=shutil.which("ffmpeg") or "",
                    frozen=bool(getattr(sys, "frozen", False)),
                )
                flush_logging()
                errors: list[str] = []
                for candidate in _funasr_load_candidates(model_name, model_ref, model_path, vad_ref, vad_path):
                    log_debug(
                        LOGGER,
                        "funasr_model_load_candidate_start",
                        requested_model=model_name,
                        candidate_source=candidate["source"],
                        candidate_model=candidate["model"],
                        candidate_vad_model=candidate["vad_model"],
                        hf_endpoint=candidate.get("hf_endpoint") or "",
                    )
                    flush_logging()
                    previous_hf_endpoint = os.environ.get("HF_ENDPOINT")
                    hf_endpoint = candidate.get("hf_endpoint")
                    if hf_endpoint:
                        os.environ["HF_ENDPOINT"] = str(hf_endpoint)
                    try:
                        _MODEL_CACHE[cache_key] = auto_model(
                            model=candidate["model"],
                            trust_remote_code=True,
                            vad_model=candidate["vad_model"],
                            vad_kwargs={"max_single_segment_time": 30000},
                            device="cpu",
                            disable_update=True,
                            **candidate["extra_kwargs"],
                        )
                        break
                    except Exception as exc:
                        errors.append(f"{candidate['source']}: {exc}")
                        log_warning(
                            LOGGER,
                            "funasr_model_load_candidate_failed",
                            requested_model=model_name,
                            candidate_source=candidate["source"],
                            candidate_model=candidate["model"],
                            candidate_vad_model=candidate["vad_model"],
                            hf_endpoint=candidate.get("hf_endpoint") or "",
                            error=repr(exc),
                        )
                        flush_logging()
                    finally:
                        if previous_hf_endpoint is None:
                            os.environ.pop("HF_ENDPOINT", None)
                        else:
                            os.environ["HF_ENDPOINT"] = previous_hf_endpoint

                if cache_key not in _MODEL_CACHE:
                    error = RuntimeError(
                        "FunASR 模型下载/加载失败，已尝试 ModelScope 与 HuggingFace 镜像候选: "
                        + "; ".join(errors[-4:])
                    )
                    log_warning(
                        LOGGER,
                        "funasr_model_load_failed",
                        requested_model=model_name,
                        resolved_model=model_ref,
                        resolved_vad_model=vad_ref,
                        error=repr(error),
                    )
                    flush_logging()
                    raise error
                log_debug(
                    LOGGER,
                    "funasr_model_load_completed",
                    requested_model=model_name,
                    resolved_model=model_ref,
                    resolved_vad_model=vad_ref,
                    cache_key=cache_key,
                )
                flush_logging()
            return _MODEL_CACHE[cache_key]

    def _resolve_model_reference(self, model_name: str) -> tuple[str, Path | None]:
        raw = str(model_name or "").strip()
        if not raw:
            return raw, None

        explicit_path = Path(raw)
        if explicit_path.exists():
            resolved = explicit_path.resolve()
            return str(resolved), resolved

        alias = _MODELSCOPE_MODEL_ALIASES.get(raw)
        if alias is not None:
            for root in self.model_roots:
                candidate = root / alias[0] / alias[1]
                if _looks_like_model_dir(candidate):
                    resolved = candidate.resolve()
                    return str(resolved), resolved

        return raw, None


def _funasr_language(language: str) -> str:
    if language in {"", "auto"}:
        return "auto"
    return language


def _bundle_base_dir(base_dir: Path) -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", base_dir / "_internal")).resolve()
    return base_dir.resolve()


def _model_roots(base_dir: Path, bundle_dir: Path) -> list[Path]:
    roots: list[Path] = []
    for root in [
        base_dir / "models" / "modelscope" / "models",
        bundle_dir / "models" / "modelscope" / "models",
    ]:
        resolved = root.resolve()
        if resolved not in roots:
            roots.append(resolved)
    return roots


def _looks_like_model_dir(path: Path) -> bool:
    if not path.is_dir():
        return False
    markers = ["configuration.json", "config.yaml", "model.pt", "am.mvn"]
    return any((path / marker).exists() for marker in markers)


def _funasr_load_candidates(
    model_name: str,
    model_ref: str,
    model_path: Path | None,
    vad_ref: str,
    vad_path: Path | None,
) -> list[dict[str, object]]:
    candidates: list[dict[str, object]] = [
        {
            "source": "modelscope/local",
            "model": model_ref,
            "vad_model": vad_ref,
            "extra_kwargs": {},
            "hf_endpoint": "",
        }
    ]

    raw = str(model_name or "").strip() or "iic/SenseVoiceSmall"
    hf_model = None
    if model_path is None:
        hf_model = _FUNASR_HF_MODEL_ALIASES.get(raw) or _FUNASR_HF_MODEL_ALIASES.get(model_ref)
    if hf_model is None:
        return candidates

    for endpoint in _huggingface_endpoints():
        label = endpoint or "default"
        candidates.append(
            {
                "source": f"huggingface:{label}",
                "model": hf_model,
                "vad_model": vad_ref if vad_path is not None else "fsmn-vad",
                "extra_kwargs": {"hub": "hf"},
                "hf_endpoint": endpoint or "",
            }
        )
    return candidates


def _huggingface_endpoints() -> list[str | None]:
    values: list[str | None] = []
    configured = os.environ.get("EASY_ASR_HF_ENDPOINTS", "")
    if configured:
        values.extend(item.strip() or None for item in configured.split(","))
    env_endpoint = os.environ.get("HF_ENDPOINT", "").strip()
    if env_endpoint:
        values.append(env_endpoint)
    values.extend(["https://hf-mirror.com", "https://huggingface.co", None])
    unique: list[str | None] = []
    for value in values:
        normalized = value.rstrip("/") if isinstance(value, str) else value
        if normalized not in unique:
            unique.append(normalized)
    return unique


def _is_remote_model_name(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    if Path(text).exists():
        return False
    return "/" in text and not any(text.startswith(prefix) for prefix in (".", "/", "\\"))


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
