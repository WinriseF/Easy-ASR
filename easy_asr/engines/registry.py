from __future__ import annotations

from pathlib import Path

from easy_asr.engines.base import EngineDescriptor
from easy_asr.engines.faster_whisper_engine import FasterWhisperEngine
from easy_asr.engines.funasr_engine import FunASRSenseVoiceEngine


class EngineRegistry:
    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
        self._engine_types = {
            FunASRSenseVoiceEngine.id: FunASRSenseVoiceEngine,
            FasterWhisperEngine.id: FasterWhisperEngine,
        }

    def descriptors(self) -> list[EngineDescriptor]:
        values = [engine_type.descriptor() for engine_type in self._engine_types.values()]
        return sorted(values, key=lambda item: item.priority)

    def create(self, engine_id: str):
        engine_type = self._engine_types.get(engine_id)
        if engine_type is None:
            raise ValueError(f"未知 ASR 引擎: {engine_id}")
        descriptor = engine_type.descriptor()
        if not descriptor.available:
            raise RuntimeError(f"{descriptor.label} 当前不可用。{descriptor.install_hint}")
        return engine_type(self.base_dir)

