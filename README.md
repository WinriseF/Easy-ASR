# Easy-ASR

Easy-ASR 现在有两种入口：

- `batch_transcribe.py`：保留原来的批量脚本，固定使用 FunASR + SenseVoiceSmall。
- `run_app.py`：新的本地浏览器工作台，后端负责 STT，前端由浏览器访问。

## 架构

```text
Browser UI
  -> FastAPI REST/SSE
    -> JobManager 单 worker 队列
      -> ASR Engine 插件
        -> FunASR SenseVoiceSmall 或 faster-whisper
        -> TerminologyLibrary 术语库增强
        -> TXT/SRT/JSON 导出
System Playback
  -> Windows WASAPI loopback capture
    -> WAV 保存
    -> JobManager 转写队列
Debug Browser
  -> Chrome DevTools Protocol
    -> 探测原始媒体 URL / HLS / DASH
    -> ffmpeg 抽取原始音频
    -> JobManager 转写队列
```

### 为什么这样做

- CPU 优先：后台只开一个转写 worker，避免多个模型同时抢 CPU。
- 模型可替换：`easy_asr/engines/` 里每个模型是独立引擎，前端只依赖统一 API。
- 性能优先：模型在进程内缓存，任务之间不会重复加载；长音频先用 ffmpeg 切片，降低内存和失败成本。
- 术语增强：术语库会生成提示词给支持 prompt/hotword 的引擎，并对输出做确定性别名替换。SenseVoiceSmall 当前主要使用后处理；faster-whisper 会使用 `initial_prompt`。
- 边听边转写：系统播放采集会边录边按约 20 秒切片提交隐藏转写任务，切片完成后把文本追加到当前片段列表。
- GPU 延后：接口里保留了模型名、量化、线程等参数，但默认只创建 CPU 引擎。

## 安装

先确保系统里能运行：

```powershell
ffmpeg -version
ffprobe -version
```

创建虚拟环境并安装服务依赖：

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
```

安装推荐 CPU ASR 后端：

```powershell
.\.venv\Scripts\python -m pip install -r requirements-asr-funasr.txt
```

可选安装 Whisper 后端：

```powershell
.\.venv\Scripts\python -m pip install -r requirements-asr-faster-whisper.txt
```

可选安装 Windows 系统播放声音采集：

```powershell
.\.venv\Scripts\python -m pip install -r requirements-capture-windows.txt
```

可选安装调试浏览器原始媒体源转写：

```powershell
.\.venv\Scripts\python -m pip install -r requirements-browser-debug.txt
```

## 运行

```powershell
.\.venv\Scripts\python run_app.py
```

浏览器打开：

```text
http://127.0.0.1:8765
```

## 使用

1. 把音频放到 `input/`，或在页面里直接上传。
2. 选择 ASR 引擎。默认建议 `SenseVoiceSmall / FunASR`，适合中文课程、访谈、会议类材料。
   也可以切到“系统播放”，选择播放设备后开始监听；录音过程中会分段实时追加转写文本，停止后会继续处理剩余切片。
   如果用浏览器播放，推荐启动调试浏览器并使用“调试浏览器 / 原始媒体源”：它会尽量提取播放前的原始媒体 URL，页面开 2 倍或 3 倍播放也不会把倍速后的声音交给 ASR。
3. 编辑右侧术语库，把专业名词写成：

```json
{
  "canonical": "随机梯度下降",
  "aliases": ["SGD", "stochastic gradient descent"],
  "weight": 1.0,
  "case_sensitive": false,
  "note": "机器学习术语"
}
```

4. 点击开始转写。
5. 任务完成后下载 TXT、SRT 或 JSON。

### 调试浏览器模式

启动一个独立 Chrome 或 Edge，并开启本地调试端口：

```powershell
chrome.exe --remote-debugging-port=9222 --user-data-dir="%TEMP%\easy-asr-chrome"
```

如果命令不在 PATH 里，可以改用 Chrome/Edge 的完整路径。也可以在 Easy-ASR 的“调试浏览器”模式里点击“启动调试浏览器”，由后端自动打开独立调试浏览器。

打开视频页面并开始播放后，在 Easy-ASR 页面里点击“标签页”，选择对应页面，再点击“监听媒体”。发现 `.m3u8`、`.mpd`、`.mp4`、`.m4a` 等候选源后，页面会自动选择最推荐的可转写源，确认后点击“原源转写”。

这个模式不会破解 DRM。遇到加密媒体、无法发现真实分片、或候选源无法被 ffmpeg 读取时，需要回退到系统音频采集。

## 目录

```text
easy_asr/
  main.py                  FastAPI 入口
  jobs.py                  任务队列和状态
  capture.py               Windows 系统播放声音采集
  browser_debug.py         调试浏览器原始媒体源探测
  audio.py                 ffmpeg/ffprobe 处理
  terminology.py           术语库
  engines/
    funasr_engine.py       SenseVoiceSmall 后端
    faster_whisper_engine.py
static/
  index.html
  styles.css
  app.js
data/terminology/default.json
input/
input/captures/
input/browser_media/
output/jobs/
chunks/_jobs/
```

## 后续适配 GPU

后续只需要在引擎层增加 `device` 选项和对应的加载策略，不需要改前端主流程和任务 API。
