from __future__ import annotations

import os
import sys
from multiprocessing import freeze_support
from pathlib import Path

import uvicorn


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

def _prepend_bundled_ffmpeg() -> None:
    runtime_dir = _runtime_base_dir()
    bundle_dir = _bundle_base_dir()

    candidates = [
        bundle_dir / "bin",
        runtime_dir / "bin",
        runtime_dir / "_internal" / "bin",
        runtime_dir / "vendor" / "ffmpeg" / "bin",
    ]

    for bin_dir in candidates:
        if (bin_dir / "ffmpeg.exe").exists() and (bin_dir / "ffprobe.exe").exists():
            os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")
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

if __name__ == "__main__":
    freeze_support()

    runtime_dir = _runtime_base_dir()
    os.chdir(runtime_dir)

    _prepend_bundled_ffmpeg()
    _print_banner()

    from easy_asr.main import app

    uvicorn.run(
        app,
        host="127.0.0.1",
        port=8765,
        reload=False,
        workers=1,
        log_level="info",
    )