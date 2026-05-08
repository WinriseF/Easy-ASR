from __future__ import annotations

import importlib.util
import inspect
import os
from pathlib import Path
from threading import Lock

from easy_asr.audio import probe_duration
from easy_asr.debug_runtime import flush_logging, get_logger, log_debug, log_warning
from easy_asr.engines.base import EngineDescriptor, EngineOptions, Segment, TranscriptionResult
from easy_asr.segmentation import sentence_segments_from_words
from easy_asr.terminology import TerminologyLibrary


_MODEL_CACHE: dict[str, object] = {}
_MODEL_LOCK = Lock()
LOGGER = get_logger(__name__)

_FASTER_WHISPER_REPOS = {
    "tiny": "Systran/faster-whisper-tiny",
    "base": "Systran/faster-whisper-base",
    "small": "Systran/faster-whisper-small",
    "medium": "Systran/faster-whisper-medium",
    "large-v1": "Systran/faster-whisper-large-v1",
    "large-v2": "Systran/faster-whisper-large-v2",
    "large-v3": "Systran/faster-whisper-large-v3",
}


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
                model_ref = self._resolve_model_reference(model_name)
                _MODEL_CACHE[cache_key] = model_cls(
                    str(model_ref),
                    device="cpu",
                    compute_type=options.compute_type or "int8",
                    cpu_threads=max(1, int(options.cpu_threads or 1)),
                )
            return _MODEL_CACHE[cache_key]

    def _resolve_model_reference(self, model_name: str) -> Path | str:
        raw = str(model_name or "small").strip() or "small"
        explicit_path = Path(raw)
        if explicit_path.exists():
            return explicit_path.resolve()

        cache_dir = self.base_dir / "models" / "faster-whisper"
        local_dir = cache_dir / _safe_model_dir_name(raw)
        if _looks_like_faster_whisper_model_dir(local_dir):
            return local_dir.resolve()

        repo_id = _FASTER_WHISPER_REPOS.get(raw)
        if repo_id is None:
            if "/" not in raw:
                raise RuntimeError(
                    f"未知 faster-whisper 模型短名: {raw}。"
                    "请选择内置模型，或填写完整 HuggingFace repo id / 本地模型目录。"
                )
            repo_id = raw
        if not _is_remote_model_name(repo_id):
            return raw

        return _download_faster_whisper_model(repo_id, local_dir)


def _download_faster_whisper_model(repo_id: str, local_dir: Path) -> Path:
    try:
        from huggingface_hub import snapshot_download
    except Exception as exc:
        raise RuntimeError("缺少 huggingface_hub，无法下载 faster-whisper 模型。") from exc

    local_dir.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    signature = inspect.signature(snapshot_download)
    accepted = set(signature.parameters)

    for endpoint in _huggingface_endpoints():
        label = endpoint or "default"
        log_debug(LOGGER, "faster_whisper_download_start", repo_id=repo_id, endpoint=label, local_dir=local_dir)
        flush_logging()
        try:
            kwargs = {
                "repo_id": repo_id,
                "local_dir": str(local_dir),
                "cache_dir": str(local_dir.parent / ".cache"),
                "resume_download": True,
            }
            if endpoint and "endpoint" in accepted:
                kwargs["endpoint"] = endpoint
            if "local_dir_use_symlinks" in accepted:
                kwargs["local_dir_use_symlinks"] = False
            snapshot_download(**{key: value for key, value in kwargs.items() if key in accepted})
            if _looks_like_faster_whisper_model_dir(local_dir):
                log_debug(LOGGER, "faster_whisper_download_completed", repo_id=repo_id, endpoint=label, local_dir=local_dir)
                flush_logging()
                return local_dir.resolve()
            errors.append(f"{label}: 下载完成但目录缺少模型文件")
        except Exception as exc:
            errors.append(f"{label}: {exc}")
            log_warning(
                LOGGER,
                "faster_whisper_download_failed",
                repo_id=repo_id,
                endpoint=label,
                local_dir=local_dir,
                error=repr(exc),
            )
            flush_logging()

    raise RuntimeError(
        "faster-whisper 模型下载失败，已尝试 HuggingFace 多个端点: "
        + "; ".join(errors[-3:])
    )


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


def _safe_model_dir_name(value: str) -> str:
    return (
        value.strip()
        .replace("\\", "__")
        .replace("/", "__")
        .replace(":", "_")
        .replace(" ", "_")
    )


def _looks_like_faster_whisper_model_dir(path: Path) -> bool:
    if not path.is_dir():
        return False
    return (path / "model.bin").exists() and (path / "config.json").exists()


def _is_remote_model_name(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    if Path(text).exists():
        return False
    return "/" in text and not any(text.startswith(prefix) for prefix in (".", "/", "\\"))


def _transcript_mode(value: str) -> str:
    return value if value in {"whole", "chunk", "sentence"} else "whole"


def _beam_size_for_preset(value: str) -> int:
    return {
        "fast": 1,
        "balanced": 3,
        "quality": 5,
    }.get(value, 3)
