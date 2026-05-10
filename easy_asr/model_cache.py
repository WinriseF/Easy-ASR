from __future__ import annotations

import gc
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Iterator, TypeVar, cast

from easy_asr.debug_runtime import flush_logging, get_logger, log_debug, log_warning


T = TypeVar("T")
Loader = Callable[[], T]
EvictCallback = Callable[[str, object], None]
LOGGER = get_logger(__name__)


@dataclass
class _CacheEntry:
    value: object | None = None
    active_users: int = 0
    last_used_at: float = 0.0
    loading: bool = False
    on_evict: EvictCallback | None = None


class ModelCache:
    def __init__(
        self,
        idle_ttl_seconds: float = 60.0,
        sweep_interval_seconds: float = 5.0,
        *,
        start_sweeper: bool = True,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.idle_ttl_seconds = max(0.0, float(idle_ttl_seconds))
        self.sweep_interval_seconds = max(1.0, float(sweep_interval_seconds))
        self._start_sweeper = start_sweeper
        self._clock = clock
        self._condition = threading.Condition(threading.RLock())
        self._entries: dict[str, _CacheEntry] = {}
        self._sweeper_thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    @contextmanager
    def lease(
        self,
        key: str,
        loader: Loader[T],
        *,
        on_evict: EvictCallback | None = None,
    ) -> Iterator[T]:
        model = self._acquire(key, loader, on_evict=on_evict)
        try:
            yield model
        finally:
            self.release(key)

    def release(self, key: str) -> None:
        with self._condition:
            entry = self._entries.get(str(key))
            if entry is None or entry.loading or entry.active_users <= 0:
                return
            entry.active_users -= 1
            entry.last_used_at = self._clock()
            self._condition.notify_all()

    def sweep_expired(self) -> list[str]:
        now = self._clock()
        expired: list[tuple[str, object, EvictCallback | None]] = []
        with self._condition:
            for key, entry in list(self._entries.items()):
                if entry.loading or entry.active_users > 0 or entry.value is None:
                    continue
                idle_seconds = now - entry.last_used_at
                if idle_seconds < self.idle_ttl_seconds:
                    continue
                value = entry.value
                on_evict = entry.on_evict
                del self._entries[key]
                expired.append((key, value, on_evict))

        for key, value, on_evict in expired:
            self._dispose(key, value, on_evict)
        return [key for key, _, _ in expired]

    def stats(self) -> dict[str, dict[str, object]]:
        now = self._clock()
        with self._condition:
            return {
                key: {
                    "active_users": entry.active_users,
                    "idle_seconds": max(0.0, now - entry.last_used_at),
                    "loading": entry.loading,
                    "loaded": entry.value is not None,
                }
                for key, entry in self._entries.items()
            }

    def close(self) -> None:
        self._stop_event.set()
        thread = self._sweeper_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2)
        expired: list[tuple[str, object, EvictCallback | None]] = []
        with self._condition:
            for key, entry in list(self._entries.items()):
                if entry.value is not None:
                    expired.append((key, entry.value, entry.on_evict))
            self._entries.clear()
            self._condition.notify_all()
        for key, value, on_evict in expired:
            self._dispose(key, value, on_evict)

    def _acquire(
        self,
        key: str,
        loader: Loader[T],
        *,
        on_evict: EvictCallback | None,
    ) -> T:
        key = str(key)
        self._ensure_sweeper()
        entry: _CacheEntry | None = None
        should_load = False

        with self._condition:
            while True:
                entry = self._entries.get(key)
                if entry is None:
                    entry = _CacheEntry(
                        loading=True,
                        last_used_at=self._clock(),
                        on_evict=on_evict,
                    )
                    self._entries[key] = entry
                    should_load = True
                    break

                if entry.loading:
                    self._condition.wait()
                    continue

                if entry.value is not None:
                    entry.active_users += 1
                    entry.last_used_at = self._clock()
                    if on_evict is not None:
                        entry.on_evict = on_evict
                    return cast(T, entry.value)

                entry.loading = True
                entry.on_evict = on_evict
                should_load = True
                break

        if not should_load or entry is None:
            raise RuntimeError(f"model cache failed to acquire entry: {key}")

        log_debug(LOGGER, "model_cache_load_start", key=key)
        flush_logging()
        try:
            value = loader()
        except Exception as exc:
            with self._condition:
                current = self._entries.get(key)
                if current is entry:
                    del self._entries[key]
                self._condition.notify_all()
            log_warning(LOGGER, "model_cache_load_failed", key=key, error=repr(exc))
            flush_logging()
            raise

        with self._condition:
            entry.value = value
            entry.loading = False
            entry.active_users = 1
            entry.last_used_at = self._clock()
            if on_evict is not None:
                entry.on_evict = on_evict
            self._condition.notify_all()

        log_debug(LOGGER, "model_cache_load_completed", key=key, model_type=type(value).__name__)
        flush_logging()
        return value

    def _ensure_sweeper(self) -> None:
        if not self._start_sweeper:
            return
        with self._condition:
            if self._sweeper_thread is not None and self._sweeper_thread.is_alive():
                return
            self._sweeper_thread = threading.Thread(
                target=self._sweep_loop,
                name="model-cache-sweeper",
                daemon=True,
            )
            self._sweeper_thread.start()

    def _sweep_loop(self) -> None:
        while not self._stop_event.wait(self.sweep_interval_seconds):
            try:
                self.sweep_expired()
            except Exception as exc:
                log_warning(LOGGER, "model_cache_sweep_failed", error=repr(exc))
                flush_logging()

    def _dispose(self, key: str, value: object, on_evict: EvictCallback | None) -> None:
        if on_evict is not None:
            try:
                on_evict(key, value)
            except Exception as exc:
                log_warning(LOGGER, "model_cache_evict_callback_failed", key=key, error=repr(exc))
        try:
            close = getattr(value, "close", None)
            if callable(close):
                close()
        except Exception as exc:
            log_warning(LOGGER, "model_cache_dispose_failed", key=key, error=repr(exc))
        finally:
            log_debug(LOGGER, "model_cache_evicted", key=key, model_type=type(value).__name__)
            flush_logging()
            del value
            gc.collect()


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(0.0, float(raw))
    except ValueError:
        return default


MODEL_CACHE = ModelCache(
    idle_ttl_seconds=_env_float("EASY_ASR_MODEL_IDLE_TTL_SECONDS", 60.0),
    sweep_interval_seconds=_env_float("EASY_ASR_MODEL_SWEEP_SECONDS", 5.0),
)
