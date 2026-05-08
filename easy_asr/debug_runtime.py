from __future__ import annotations

import faulthandler
import json
import logging
import os
import platform
import sys
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Any

_LOCK = RLock()
_LOG_PATH: Path | None = None
_FAULT_PATH: Path | None = None
_FAULT_HANDLE = None


def runtime_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def package_log_dir() -> Path:
    path = runtime_base_dir() / "package_logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def configure_debug_logging() -> Path:
    global _LOG_PATH

    with _LOCK:
        if _LOG_PATH is not None:
            return _LOG_PATH

        log_path = package_log_dir() / f"runtime_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{os.getpid()}.log"
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.DEBUG)

        handler = logging.FileHandler(log_path, encoding="utf-8")
        handler.set_name("easy_asr_debug_file")
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s [%(levelname)s] %(process)d %(threadName)s %(name)s: %(message)s"
            )
        )
        root_logger.addHandler(handler)
        logging.captureWarnings(True)

        _LOG_PATH = log_path

    logger = logging.getLogger(__name__)
    logger.debug("debug_logging_configured | %s", json_dumps(runtime_snapshot()))
    flush_logging()
    return log_path


def install_faulthandler() -> Path:
    global _FAULT_HANDLE, _FAULT_PATH

    with _LOCK:
        if _FAULT_PATH is not None:
            return _FAULT_PATH

        fault_path = package_log_dir() / f"fatal_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{os.getpid()}.log"
        handle = open(fault_path, "a", encoding="utf-8")
        handle.write(f"faulthandler enabled at {datetime.now().isoformat(timespec='seconds')}\n")
        handle.flush()
        faulthandler.enable(file=handle, all_threads=True)

        _FAULT_HANDLE = handle
        _FAULT_PATH = fault_path

    logging.getLogger(__name__).debug("faulthandler_enabled | %s", json_dumps({"fault_log_path": str(_FAULT_PATH)}))
    flush_logging()
    return fault_path


def current_log_path() -> str:
    return str(_LOG_PATH) if _LOG_PATH else ""


def current_fault_path() -> str:
    return str(_FAULT_PATH) if _FAULT_PATH else ""


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log_debug(logger: logging.Logger, event: str, **fields: Any) -> None:
    logger.debug("%s | %s", event, json_dumps(fields))


def log_info(logger: logging.Logger, event: str, **fields: Any) -> None:
    logger.info("%s | %s", event, json_dumps(fields))


def log_warning(logger: logging.Logger, event: str, **fields: Any) -> None:
    logger.warning("%s | %s", event, json_dumps(fields))


def flush_logging() -> None:
    for handler in logging.getLogger().handlers:
        try:
            handler.flush()
        except Exception:
            pass


def runtime_snapshot() -> dict[str, Any]:
    path_entries = [entry for entry in os.environ.get("PATH", "").split(os.pathsep) if entry]
    selected_env = {
        key: os.environ.get(key, "")
        for key in [
            "PATH",
            "PYTHONPATH",
            "PYTHONHOME",
            "CONDA_PREFIX",
            "SSL_CERT_FILE",
            "REQUESTS_CA_BUNDLE",
            "EASY_ASR_FFMPEG_EXE",
            "EASY_ASR_FFPROBE_EXE",
            "EASY_ASR_FFMPEG_DIR",
            "FFMPEG_BINARY",
            "FFPROBE_BINARY",
            "MODELSCOPE_CACHE",
            "HF_HOME",
            "TORCH_HOME",
            "MODELSCOPE_OFFLINE",
            "HF_HUB_OFFLINE",
        ]
        if os.environ.get(key)
    }
    if "PATH" in selected_env:
        selected_env["PATH"] = path_entries[:12]

    return {
        "argv": sys.argv,
        "cwd": str(Path.cwd()),
        "executable": sys.executable,
        "frozen": bool(getattr(sys, "frozen", False)),
        "meipass": str(getattr(sys, "_MEIPASS", "")),
        "pid": os.getpid(),
        "platform": platform.platform(),
        "python": sys.version,
        "runtime_dir": str(runtime_base_dir()),
        "selected_env": selected_env,
    }


def shorten(value: Any, limit: int = 300) -> str:
    text = str(value)
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def json_dumps(payload: Any) -> str:
    return json.dumps(_normalize(payload), ensure_ascii=False, sort_keys=True)


def _normalize(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _normalize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_normalize(item) for item in value]
    return value
