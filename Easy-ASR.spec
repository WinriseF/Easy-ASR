# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_all, copy_metadata


project_dir = Path.cwd()


def safe_collect_all(package_name: str):
    try:
        datas, binaries, hiddenimports = collect_all(package_name)
        return datas, binaries, hiddenimports
    except Exception:
        return [], [], []


def safe_copy_metadata(distribution_name: str):
    try:
        return copy_metadata(distribution_name)
    except Exception:
        return []


datas = []
binaries = []
hiddenimports = [
    "easy_asr.main",
    "easy_asr.engines.registry",
    "easy_asr.engines.funasr_engine",
    "easy_asr.engines.faster_whisper_engine",

    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",

    "multipart",
    "multipart.multipart",
    "python_multipart",
]


# 静态前端资源
datas += [
    (str(project_dir / "static"), "static"),
]

# 默认术语库，打进去作为只读资源；运行时 data 目录仍会在 exe 旁边创建
default_terms = project_dir / "data" / "terminology" / "default.json"
if default_terms.exists():
    datas += [
        (str(default_terms), "data/terminology"),
    ]


# ffmpeg / ffprobe：作为普通数据文件带进去，不让 PyInstaller 当二进制依赖扫描
ffmpeg_dir = project_dir / "vendor" / "ffmpeg" / "bin"
for name in ["ffmpeg.exe", "ffprobe.exe"]:
    path = ffmpeg_dir / name
    if path.exists():
        datas += [
            (str(path), "bin"),
        ]


# 基础 Web 服务依赖
base_packages = [
    "fastapi",
    "starlette",
    "pydantic",
    "uvicorn",
    "anyio",
    "sniffio",
    "h11",
]

# 可选功能依赖：装了就收进去，没装就跳过
optional_packages = [
    # 表单上传
    "python_multipart",

    # FunASR / SenseVoice
    "funasr",
    "modelscope",
    "torch",
    "torchaudio",

    # faster-whisper
    "faster_whisper",
    "ctranslate2",

    # 浏览器调试 / 原始媒体
    "websocket",
    "yt_dlp",

    # Windows 系统声音采集
    "pyaudiowpatch",

    # 常见底层依赖
    "numpy",
    "soundfile",
    "yaml",
]


for package in base_packages + optional_packages:
    d, b, h = safe_collect_all(package)
    datas += d
    binaries += b
    hiddenimports += h


# 部分包依赖 importlib.metadata 读取版本信息
metadata_distributions = [
    "fastapi",
    "starlette",
    "pydantic",
    "uvicorn",
    "python-multipart",
    "funasr",
    "modelscope",
    "torch",
    "torchaudio",
    "faster-whisper",
    "ctranslate2",
    "websocket-client",
    "yt-dlp",
    "PyAudioWPatch",
]

for dist in metadata_distributions:
    datas += safe_copy_metadata(dist)


hiddenimports = sorted(set(hiddenimports))


a = Analysis(
    ["run_app.py"],
    pathex=[str(project_dir)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Easy-ASR",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Easy-ASR",
)