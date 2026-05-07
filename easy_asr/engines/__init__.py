__all__ = ["EngineRegistry"]


def __getattr__(name: str):
    if name == "EngineRegistry":
        from .registry import EngineRegistry

        return EngineRegistry
    raise AttributeError(name)
