from __future__ import annotations

import os
import sys
import threading
import traceback
from datetime import datetime
from multiprocessing import freeze_support
from pathlib import Path

from easy_asr.debug_runtime import (
    configure_debug_logging,
    current_fault_path,
    current_log_path,
    flush_logging,
    get_logger,
    install_faulthandler,
    log_debug,
    runtime_snapshot,
)

DEBUG_LOG_PATH: Path | None = None
FAULT_LOG_PATH: Path | None = None
LOGGER = get_logger(__name__)

def _runtime_base_dir() -> Path:
    """
    运行时可写目录：
    - 源码运行：项目根目录
    - 打包运行：exe 所在目录
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _bundle_base_dir() -> Path:
    """
    打包资源目录：
    - 源码运行：项目根目录
    - PyInstaller onedir：通常是 exe 旁边的 _internal
    """
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", _runtime_base_dir() / "_internal"))
    return Path(__file__).resolve().parent


def _warm_up_python_ssl() -> None:
    info: dict[str, object] = {}
    try:
        import ssl
        info["openssl_version"] = ssl.OPENSSL_VERSION
        try:
            info["default_verify_paths"] = ssl.get_default_verify_paths()._asdict()
        except Exception as exc:
            info["default_verify_paths_error"] = str(exc)
    except Exception as exc:
        info["ssl_import_error"] = str(exc)

    try:
        import certifi

        info["certifi_version"] = getattr(certifi, "__version__", "")
        info["certifi_where"] = certifi.where()
    except Exception as exc:
        info["certifi_error"] = str(exc)

    log_debug(LOGGER, "ssl_runtime_probe", **info)
    flush_logging()


def _configure_bundled_ffmpeg() -> None:
    runtime_dir = _runtime_base_dir()
    bundle_dir = _bundle_base_dir()

    candidates = [
        bundle_dir / "bin",
        runtime_dir / "bin",
        runtime_dir / "_internal" / "bin",
        runtime_dir / "vendor" / "ffmpeg" / "bin",
    ]

    for bin_dir in candidates:
        ffmpeg = bin_dir / "ffmpeg.exe"
        ffprobe = bin_dir / "ffprobe.exe"

        if ffmpeg.exists() and ffprobe.exists():
            existing_path = os.environ.get("PATH", "")
            path_entries = [entry for entry in existing_path.split(os.pathsep) if entry]
            bin_dir_text = str(bin_dir)
            if not any(Path(entry).resolve() == bin_dir.resolve() for entry in path_entries if entry):
                os.environ["PATH"] = bin_dir_text + (os.pathsep + existing_path if existing_path else "")
            os.environ["EASY_ASR_FFMPEG_EXE"] = str(ffmpeg)
            os.environ["EASY_ASR_FFPROBE_EXE"] = str(ffprobe)
            os.environ["EASY_ASR_FFMPEG_DIR"] = str(bin_dir)
            os.environ.setdefault("FFMPEG_BINARY", str(ffmpeg))
            os.environ.setdefault("FFPROBE_BINARY", str(ffprobe))
            log_debug(
                LOGGER,
                "bundled_ffmpeg_selected",
                runtime_dir=runtime_dir,
                bundle_dir=bundle_dir,
                selected_dir=bin_dir,
                ffmpeg=ffmpeg,
                ffprobe=ffprobe,
                path_prepended=True,
                path_head=os.environ.get("PATH", "").split(os.pathsep)[:5],
            )
            flush_logging()
            return

    log_debug(LOGGER, "bundled_ffmpeg_not_found", runtime_dir=runtime_dir, bundle_dir=bundle_dir, candidates=candidates)
    flush_logging()


def _print_banner() -> None:
    banner = r"""
  ______    ___    _____ __   __          ___      _____ _____
 |  ____|  / \ \  / / __|\ \ / /         / \ \    / / __|  __ \
 | |__    / _ \ \/ /\__ \ \ V /  _____  / _ \ \  / /\__ \ |__) |
 |  __|  / ___ \  /  __) | | |  |_____|/ ___ \ \/ /  __) |  _  /
 | |____/_/   \_\/  |____/  |_|       /_/   \_\  /  |____/| | \ \
 |______|                                EASY-ASR  |_|        |_|  \_\

    Local Speech-to-Text Workstation
    Web UI : http://127.0.0.1:8765
    Mode   : FastAPI + Uvicorn + Local ASR
"""
    print(banner, flush=True)


def _write_crash_log(title: str, detail: str) -> Path:
    log_dir = _runtime_base_dir() / "package_logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    log_path = log_dir / f"crash_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

    content = f"""EASY-ASR Crash Log
Time       : {datetime.now().isoformat(timespec="seconds")}
Title      : {title}
Frozen     : {getattr(sys, "frozen", False)}
Executable : {sys.executable}
CWD        : {Path.cwd()}
RuntimeDir : {_runtime_base_dir()}
BundleDir  : {_bundle_base_dir()}
Python     : {sys.version}
DebugLog   : {current_log_path()}
FatalLog   : {current_fault_path()}

==================== Traceback ====================

{detail}
"""

    log_path.write_text(content, encoding="utf-8")
    return log_path


def _report_exception(exc_type, exc_value, exc_traceback, title: str = "EASY-ASR 崩溃") -> None:
    detail = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
    LOGGER.error("%s | %s", title, detail)
    flush_logging()

    print("\n" + "=" * 80, file=sys.stderr)
    print(f"{title}", file=sys.stderr)
    print("=" * 80, file=sys.stderr)
    print(detail, file=sys.stderr)

    try:
        log_path = _write_crash_log(title, detail)
        print(f"\n错误日志已保存到：{log_path}", file=sys.stderr)
    except Exception as log_error:
        print(f"\n错误日志写入失败：{log_error}", file=sys.stderr)


def _install_exception_hooks() -> None:
    def handle_main_exception(exc_type, exc_value, exc_traceback):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return
        _report_exception(exc_type, exc_value, exc_traceback, "EASY-ASR 未处理异常")

    sys.excepthook = handle_main_exception

    if hasattr(threading, "excepthook"):
        def handle_thread_exception(args):
            if args.exc_type is SystemExit:
                return
            thread_name = args.thread.name if args.thread else "unknown-thread"
            _report_exception(
                args.exc_type,
                args.exc_value,
                args.exc_traceback,
                f"EASY-ASR 后台线程异常：{thread_name}",
            )

        threading.excepthook = handle_thread_exception


def _pause_before_exit() -> None:
    print("\n程序异常退出。为了方便查看错误原因，窗口不会自动关闭。", file=sys.stderr)
    try:
        input("按 Enter 退出...")
    except (EOFError, OSError):
        pass


def _maybe_run_ytdlp_worker() -> bool:
    if len(sys.argv) < 2 or sys.argv[1] != "--easy-asr-ytdlp":
        return False
    if len(sys.argv) != 3:
        raise RuntimeError("yt-dlp worker requires a config path.")

    log_debug(LOGGER, "ytdlp_worker_mode_entered", config_path=sys.argv[2], **runtime_snapshot())
    flush_logging()

    from easy_asr.ytdlp_worker import run_from_config

    exit_code = run_from_config(sys.argv[2])
    raise SystemExit(exit_code)


if __name__ == "__main__":
    _install_exception_hooks()

    try:
        os.environ.setdefault("PYTHONFAULTHANDLER", "1")
        DEBUG_LOG_PATH = configure_debug_logging()
        FAULT_LOG_PATH = install_faulthandler()
        log_debug(LOGGER, "process_bootstrap", debug_log_path=DEBUG_LOG_PATH, fault_log_path=FAULT_LOG_PATH, **runtime_snapshot())
        flush_logging()

        freeze_support()

        runtime_dir = _runtime_base_dir()
        os.chdir(runtime_dir)
        log_debug(LOGGER, "runtime_directory_changed", runtime_dir=runtime_dir, cwd=Path.cwd())

        _warm_up_python_ssl()
        _configure_bundled_ffmpeg()

        _maybe_run_ytdlp_worker()

        _print_banner()

        from easy_asr.main import app
        import uvicorn

        log_debug(
            LOGGER,
            "uvicorn_starting",
            host="127.0.0.1",
            port=8765,
            ffmpeg=os.environ.get("EASY_ASR_FFMPEG_EXE", ""),
            ffprobe=os.environ.get("EASY_ASR_FFPROBE_EXE", ""),
            ffmpeg_dir=os.environ.get("EASY_ASR_FFMPEG_DIR", ""),
        )
        flush_logging()

        uvicorn.run(
            app,
            host="127.0.0.1",
            port=8765,
            reload=False,
            workers=1,
            log_level="info",
            use_colors=False,
        )

    except KeyboardInterrupt:
        print("\nEasy-ASR 已停止。")
    except Exception as exc:
        _report_exception(type(exc), exc, exc.__traceback__, "EASY-ASR 启动或运行失败")
        _pause_before_exit()
        sys.exit(1)
