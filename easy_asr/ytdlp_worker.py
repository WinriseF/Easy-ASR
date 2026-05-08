from __future__ import annotations

import ctypes.util
import json
import os
import ssl
import sys
from pathlib import Path
from typing import Any

from easy_asr.debug_runtime import (
    configure_debug_logging,
    flush_logging,
    get_logger,
    install_faulthandler,
    log_debug,
    runtime_snapshot,
    shorten,
)

LOGGER = get_logger(__name__)


class YtdlpWorkerLogger:
    def __init__(self, import_id: str):
        self.import_id = import_id

    def debug(self, message: str) -> None:
        if message.startswith("[debug] ") or message.startswith("["):
            log_debug(LOGGER, "ytdlp_worker_debug", import_id=self.import_id, message=shorten(message, 1200))
            flush_logging()

    def warning(self, message: str) -> None:
        log_debug(LOGGER, "ytdlp_worker_warning", import_id=self.import_id, message=shorten(message, 1200))
        flush_logging()

    def error(self, message: str) -> None:
        log_debug(LOGGER, "ytdlp_worker_error", import_id=self.import_id, message=shorten(message, 1200))
        flush_logging()


def run_from_config(config_path: str) -> int:
    configure_debug_logging()
    install_faulthandler()

    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    import_id = str(config.get("import_id") or "")

    _log_native_runtime(import_id)

    try:
        import yt_dlp as ytdlp
    except Exception as exc:
        log_debug(LOGGER, "ytdlp_worker_import_failed", import_id=import_id, error=repr(exc))
        flush_logging()
        raise

    options = _build_options(config)
    log_debug(
        LOGGER,
        "ytdlp_worker_start",
        import_id=import_id,
        source_url=shorten(config.get("source_url", ""), 500),
        media_path=config.get("media_path", ""),
        output_template=config.get("output_template", ""),
        ytdlp_runtime=_yt_dlp_runtime_summary(ytdlp),
        options=_summarize_options(options),
    )
    flush_logging()

    with ytdlp.YoutubeDL(options) as downloader:
        downloader.download([str(config["source_url"])])

    media_path = Path(config["media_path"])
    log_debug(
        LOGGER,
        "ytdlp_worker_finished",
        import_id=import_id,
        media_path=media_path,
        exists=media_path.exists(),
        size=media_path.stat().st_size if media_path.exists() else 0,
    )
    flush_logging()
    return 0


def _build_options(config: dict[str, Any]) -> dict[str, Any]:
    import_id = str(config.get("import_id") or "")
    mode = str(config.get("mode") or "extract_audio")
    kind = str(config.get("kind") or "audio")
    ffmpeg_dir = str(config.get("ffmpeg_dir") or "")
    headers = dict(config.get("headers") or {})

    options: dict[str, Any] = {
        "format": "bestvideo*+bestaudio/best" if kind == "video" else "bestaudio/best",
        "outtmpl": str(config["output_template"]),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": False,
        "verbose": True,
        "logger": YtdlpWorkerLogger(import_id),
        "progress_hooks": [_make_progress_hook(import_id)],
    }
    if headers:
        options["http_headers"] = headers
    if ffmpeg_dir:
        options["ffmpeg_location"] = ffmpeg_dir
    if kind == "video":
        options["merge_output_format"] = "mp4"
    elif mode == "download":
        options["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "m4a",
                "preferredquality": "0",
            }
        ]
    else:
        options["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "wav",
                "preferredquality": "0",
            }
        ]
    return options


def _make_progress_hook(import_id: str):
    def hook(payload: dict[str, Any]) -> None:
        log_debug(
            LOGGER,
            "ytdlp_worker_progress",
            import_id=import_id,
            status=payload.get("status", ""),
            downloaded_bytes=payload.get("downloaded_bytes"),
            total_bytes=payload.get("total_bytes"),
            total_bytes_estimate=payload.get("total_bytes_estimate"),
            eta=payload.get("eta"),
            speed=payload.get("speed"),
            filename=shorten(payload.get("filename", ""), 260),
        )
        flush_logging()

    return hook


def _log_native_runtime(import_id: str) -> None:
    info: dict[str, Any] = {
        "runtime": runtime_snapshot(),
        "find_library_ssl": ctypes.util.find_library("ssl"),
        "find_library_crypto": ctypes.util.find_library("crypto"),
    }
    try:
        import _ssl

        info["_ssl_file"] = getattr(_ssl, "__file__", "")
    except Exception as exc:
        info["_ssl_error"] = repr(exc)
    try:
        info["openssl_version"] = ssl.OPENSSL_VERSION
        info["default_verify_paths"] = ssl.get_default_verify_paths()._asdict()
    except Exception as exc:
        info["ssl_error"] = repr(exc)
    try:
        import certifi

        info["certifi_file"] = getattr(certifi, "__file__", "")
        info["certifi_version"] = getattr(certifi, "__version__", "")
        info["certifi_where"] = certifi.where()
    except Exception as exc:
        info["certifi_error"] = repr(exc)
    log_debug(LOGGER, "ytdlp_worker_native_runtime", import_id=import_id, **info)
    flush_logging()


def _module_summary(module: Any) -> dict[str, str]:
    if module is None:
        return {"present": "false"}
    return {
        "present": "true",
        "file": shorten(getattr(module, "__file__", ""), 260),
        "version": str(getattr(module, "__version__", "")),
    }


def _yt_dlp_runtime_summary(ytdlp: Any) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "module_file": shorten(getattr(ytdlp, "__file__", ""), 260),
        "module_version": str(getattr(ytdlp, "__version__", "")),
    }
    try:
        from yt_dlp import dependencies as deps

        summary["dependencies"] = {
            name: _module_summary(getattr(deps, name, None))
            for name in ["certifi", "curl_cffi", "requests", "urllib3", "websockets", "brotli", "Cryptodome"]
        }
    except Exception as exc:
        summary["dependencies_error"] = str(exc)
    return summary


def _summarize_options(options: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for key, value in options.items():
        if key in {"logger", "progress_hooks"}:
            summary[key] = f"<{key}>"
        elif key == "http_headers":
            headers = dict(value)
            if "Cookie" in headers:
                headers["Cookie"] = f"<cookie len={len(headers['Cookie'])}>"
            summary[key] = headers
        else:
            summary[key] = shorten(value, 260)
    return summary


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1:
        raise SystemExit("Usage: ytdlp_worker <config.json>")
    return run_from_config(argv[0])


if __name__ == "__main__":
    raise SystemExit(main())
