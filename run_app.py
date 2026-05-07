from __future__ import annotations

import os
import sys
from pathlib import Path

import uvicorn


def _prepend_bundled_ffmpeg() -> None:
    if getattr(sys, "frozen", False):
        base_dir = Path(sys.executable).resolve().parent
        bundle_dir = Path(getattr(sys, "_MEIPASS", base_dir))
    else:
        base_dir = Path(__file__).resolve().parent
        bundle_dir = base_dir

    candidates = [
        bundle_dir / "bin",
        base_dir / "bin",
        base_dir / "_internal" / "bin",
        base_dir / "vendor" / "ffmpeg" / "bin",
    ]
    for bin_dir in candidates:
        if (bin_dir / "ffmpeg.exe").exists() and (bin_dir / "ffprobe.exe").exists():
            os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")
            return


if __name__ == "__main__":
    _prepend_bundled_ffmpeg()
    uvicorn.run("easy_asr.main:app", host="127.0.0.1", port=8765, reload=False)
