import os
import sys
import shutil
import subprocess
from pathlib import Path

# =========================
# 基础配置
# =========================

BASE_DIR = Path(__file__).resolve().parent

INPUT_DIR = BASE_DIR / "input"
OUTPUT_DIR = BASE_DIR / "output"
CHUNKS_DIR = BASE_DIR / "chunks"
PARTS_DIR = OUTPUT_DIR / "_parts"

CHUNK_SECONDS = 10 * 60  # 10 分钟一段

AUDIO_EXTS = {
    ".mp3", ".m4a", ".wav", ".flac", ".aac", ".ogg", ".mp4", ".mkv"
}

# 把模型缓存放到当前项目目录，方便以后整个文件夹删除
os.environ["MODELSCOPE_CACHE"] = str(BASE_DIR / "models" / "modelscope")
os.environ["HF_HOME"] = str(BASE_DIR / "models" / "hf")
os.environ["TORCH_HOME"] = str(BASE_DIR / "models" / "torch")


# =========================
# 导入 FunASR
# =========================

from funasr import AutoModel
from funasr.utils.postprocess_utils import rich_transcription_postprocess


def ensure_dirs():
    INPUT_DIR.mkdir(exist_ok=True)
    OUTPUT_DIR.mkdir(exist_ok=True)
    CHUNKS_DIR.mkdir(exist_ok=True)
    PARTS_DIR.mkdir(exist_ok=True)
    (BASE_DIR / "models").mkdir(exist_ok=True)


def check_ffmpeg():
    if shutil.which("ffmpeg") is None:
        print("没有找到 ffmpeg。")
        print("请先安装 ffmpeg，并确保在 CMD 里运行 ffmpeg -version 能看到版本信息。")
        sys.exit(1)


def run_cmd(cmd):
    subprocess.run(cmd, check=True)


def find_audio_files():
    files = []
    for p in INPUT_DIR.iterdir():
        if p.is_file() and p.suffix.lower() in AUDIO_EXTS:
            files.append(p)
    return sorted(files)


def split_audio_to_chunks(audio_path: Path):
    """
    把原始音频切成 10 分钟 wav 小块。
    如果 chunks 已经存在，则跳过切分，方便断点续跑。
    """
    stem = audio_path.stem
    chunk_dir = CHUNKS_DIR / stem
    chunk_dir.mkdir(parents=True, exist_ok=True)

    existing_chunks = sorted(chunk_dir.glob("*.wav"))
    if existing_chunks:
        print(f"已存在切片，跳过切分: {stem}")
        return existing_chunks

    print(f"开始切分: {audio_path.name}")

    output_pattern = str(chunk_dir / f"{stem}_part_%03d.wav")

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-i", str(audio_path),
        "-vn",
        "-ac", "1",
        "-ar", "16000",
        "-c:a", "pcm_s16le",
        "-f", "segment",
        "-segment_time", str(CHUNK_SECONDS),
        "-reset_timestamps", "1",
        output_pattern,
    ]

    run_cmd(cmd)

    chunks = sorted(chunk_dir.glob("*.wav"))
    if not chunks:
        raise RuntimeError(f"切分失败，没有生成切片: {audio_path}")

    print(f"切分完成: {audio_path.name} -> {len(chunks)} 段")
    return chunks


def load_model():
    print("正在加载 SenseVoiceSmall 模型...")
    print("第一次运行会下载模型，之后会使用本地缓存。")
    print("当前模式: CPU 推理")

    model = AutoModel(
        model="iic/SenseVoiceSmall",
        trust_remote_code=True,
        vad_model="fsmn-vad",
        vad_kwargs={
            # 每个语音片段最长 30 秒，长音频更稳定
            "max_single_segment_time": 30000
        },
        device="cpu",
        disable_update=True,
    )

    print("模型加载完成")
    return model


def transcribe_chunk(model, chunk_path: Path, part_txt_path: Path):
    """
    转写单个切片。
    如果该切片 txt 已存在，则跳过，方便断点续跑。
    """
    if part_txt_path.exists() and part_txt_path.stat().st_size > 0:
        print(f"已存在转写结果，跳过: {part_txt_path.name}")
        return part_txt_path.read_text(encoding="utf-8").strip()

    print(f"开始转写切片: {chunk_path.name}")

    result = model.generate(
        input=str(chunk_path),
        cache={},
        language="zh",
        use_itn=True,
        batch_size_s=60,
        merge_vad=True,
        merge_length_s=15,
    )

    texts = []

    for item in result:
        text = item.get("text", "")
        text = rich_transcription_postprocess(text)
        text = text.strip()
        if text:
            texts.append(text)

    final_text = "\n".join(texts).strip()

    part_txt_path.write_text(final_text, encoding="utf-8")

    print(f"切片完成: {part_txt_path.name}")
    return final_text


def transcribe_one_file(model, audio_path: Path):
    stem = audio_path.stem

    print("=" * 60)
    print(f"处理文件: {audio_path.name}")
    print("=" * 60)

    chunks = split_audio_to_chunks(audio_path)

    file_parts_dir = PARTS_DIR / stem
    file_parts_dir.mkdir(parents=True, exist_ok=True)

    merged_texts = []

    for index, chunk_path in enumerate(chunks, start=1):
        part_txt_path = file_parts_dir / f"{stem}_part_{index:03d}.txt"
        text = transcribe_chunk(model, chunk_path, part_txt_path)

        if text:
            merged_texts.append(text)

    merged_text = "\n\n".join(merged_texts).strip()

    output_txt = OUTPUT_DIR / f"{stem}.txt"
    output_txt.write_text(merged_text, encoding="utf-8")

    print(f"合并完成: {output_txt}")
    return output_txt


def main():
    ensure_dirs()
    check_ffmpeg()

    audio_files = find_audio_files()

    if not audio_files:
        print(f"没有在 input 文件夹里找到音频文件: {INPUT_DIR}")
        print("请把 mp3 放进 input 文件夹后重新运行。")
        return

    print(f"发现 {len(audio_files)} 个音频文件:")
    for f in audio_files:
        print(f" - {f.name}")

    model = load_model()

    completed = []

    for audio_path in audio_files:
        try:
            out = transcribe_one_file(model, audio_path)
            completed.append(out)
        except Exception as e:
            print(f"处理失败: {audio_path.name}")
            print(f"错误信息: {e}")

    print("=" * 60)
    print("全部任务结束")
    print("=" * 60)

    if completed:
        print("已生成:")
        for p in completed:
            print(f" - {p}")


if __name__ == "__main__":
    main()