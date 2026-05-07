from __future__ import annotations

import os
import sys
import threading
import traceback
from datetime import datetime
from multiprocessing import freeze_support
from pathlib import Path

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
    try:
        import ssl
        _ = ssl.OPENSSL_VERSION
    except Exception:
        pass


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
            os.environ["EASY_ASR_FFMPEG_EXE"] = str(ffmpeg)
            os.environ["EASY_ASR_FFPROBE_EXE"] = str(ffprobe)
            os.environ["EASY_ASR_FFMPEG_DIR"] = str(bin_dir)
            return


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

==================== Traceback ====================

{detail}
"""

    log_path.write_text(content, encoding="utf-8")
    return log_path


def _report_exception(exc_type, exc_value, exc_traceback, title: str = "EASY-ASR 崩溃") -> None:
    detail = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))

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


if __name__ == "__main__":
    _install_exception_hooks()

    try:
        freeze_support()

        runtime_dir = _runtime_base_dir()
        os.chdir(runtime_dir)

        _warm_up_python_ssl()
        _configure_bundled_ffmpeg()
        _print_banner()

        from easy_asr.main import app
        import uvicorn

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