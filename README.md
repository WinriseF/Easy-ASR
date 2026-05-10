# Easy-ASR

本地语音转写工作站 | Local Speech-to-Text Workstation

Easy-ASR 是一个面向中文长音频、课程、会议、访谈和在线视频素材的本地语音转写工具。项目提供浏览器中的 Web 工作台，也保留了命令行批处理入口；音频、模型缓存、转写结果和日志都存放在本机目录内，适合需要隐私、可控成本和可离线复用的个人或小团队工作流。

项目当前以 Windows 本地使用为主要目标，核心服务基于 FastAPI + Uvicorn，前端为原生静态页面，ASR 后端支持 FunASR SenseVoiceSmall 和 faster-whisper，并围绕长音频切片、系统播放录音、浏览器原始媒体探测、术语库增强和多格式导出做了完整封装。

## 核心能力

| 能力 | 说明 |
| --- | --- |
| 本地 Web 工作台 | 启动后访问 `http://127.0.0.1:8765`，在浏览器中完成上传、转写、实时进度查看和结果下载。 |
| 双 ASR 引擎 | 默认推荐 `FunASR SenseVoiceSmall`，也支持 `faster-whisper` 的 tiny/base/small/medium/large-v3 等模型。 |
| 长音频切片 | 通过 ffmpeg 将音频切成可控长度的 16kHz 单声道 WAV，降低长文件处理失败率。 |
| 系统播放录音 | 基于 Windows WASAPI loopback 采集电脑正在播放的声音，并按实时切片追加转写结果。 |
| 调试浏览器探测 | 自动启动 Chrome/Edge 调试浏览器，通过 Chrome DevTools Protocol 探测页面中的 HLS、DASH、MP4、M4A、MP3 等媒体源。 |
| 平台页面提取 | 对 YouTube、Bilibili 等常见站点提供 `yt-dlp` 候选提取兜底，用于处理 blob/MSE 自适应流页面。 |
| 术语库增强 | 支持维护专业术语、别名和大小写策略，FunASR 使用后处理增强，faster-whisper 使用 prompt 注入。 |
| 多格式导出 | 支持 `TXT`、`SRT`、`VTT`、`TSV`、`CSV`、`JSON`，覆盖文稿、字幕和结构化数据场景。 |
| 模型预下载 | Web UI 中可提前触发模型加载/下载，减少正式任务开始后的等待时间。 |
| 打包运行支持 | 入口文件兼容源码运行和 PyInstaller onedir 打包运行，并支持随包查找 `bin/ffmpeg.exe`、`bin/ffprobe.exe`。 |

## 适用场景

- 中文课程、讲座、播客、访谈、会议录音转文字。
- 长视频、长音频离线转写，导出字幕或结构化分段。
- 需要本地处理、不希望上传音频到云端的工作流。
- 在线网页音视频素材转写，优先尝试抓取原始媒体，抓不到时可回退系统播放录音。
- 带专业名词、课程术语、产品名或人名的转写任务。

## 项目截图

仓库中已包含 Web UI 资源：`static/index.html`、`static/styles.css`、`static/app.js`。如果要在 GitHub README 中展示界面截图，建议将截图放到 `docs/images/` 后在这里引用，例如：

```markdown
![Easy-ASR Web UI](docs/images/web-ui.png)
```

## 技术架构

```text
Browser UI
  |
  |-- FastAPI REST API
  |     |
  |     |-- JobManager
  |     |     |-- single worker queue
  |     |     |-- job history
  |     |     |-- SSE progress events
  |     |
  |     |-- ASR engines
  |     |     |-- FunASR SenseVoiceSmall
  |     |     |-- faster-whisper
  |     |
  |     |-- terminology library
  |     |-- output writers
  |     |-- model preload tasks
  |
  |-- File transcription
  |     |-- upload / input directory
  |     |-- ffmpeg split
  |     |-- TXT / SRT / VTT / TSV / CSV / JSON
  |
  |-- System playback capture
  |     |-- Windows WASAPI loopback
  |     |-- 20s live chunks
  |     |-- incremental transcript
  |
  |-- Debug browser media import
        |-- Chrome DevTools Protocol
        |-- media element / performance / network candidates
        |-- ffprobe validation
        |-- ffmpeg extraction
        |-- yt-dlp fallback
```

设计上，Easy-ASR 使用单 worker 队列处理 ASR 任务，避免多个模型同时抢占 CPU 和内存。每个任务都有独立输出目录，并写入 `_job.json` 保存任务状态、参数、分段和输出文件路径，服务重启后可以恢复历史记录。

## 快速开始

### 1. 准备环境

基础要求：

- Python 3.10+
- ffmpeg / ffprobe
- Windows 10/11（系统播放录音、调试浏览器自动启动主要面向 Windows）
- Chrome 或 Microsoft Edge（使用调试浏览器功能时需要）

如果使用源码运行，请确认 `ffmpeg` 和 `ffprobe` 已加入 `PATH`：

```powershell
ffmpeg -version
ffprobe -version
```

如果使用打包版本，程序会优先查找以下目录中的 ffmpeg：

- `bin/ffmpeg.exe`
- `_internal/bin/ffmpeg.exe`
- `vendor/ffmpeg/bin/ffmpeg.exe`

### 2. 创建虚拟环境

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\python -m pip install -r requirements.txt
```

### 3. 安装 ASR 引擎

推荐中文转写使用 FunASR SenseVoiceSmall：

```powershell
.\.venv\Scripts\python -m pip install -r requirements-asr-funasr.txt
```

如果你更熟悉 Whisper，或希望使用 faster-whisper 的时间戳与模型选择：

```powershell
.\.venv\Scripts\python -m pip install -r requirements-asr-faster-whisper.txt
```

两个引擎可以同时安装。Web UI 会自动检测可用引擎，并展示安装提示。

### 4. 安装可选功能依赖

系统播放录音：

```powershell
.\.venv\Scripts\python -m pip install -r requirements-capture-windows.txt
```

调试浏览器媒体探测和平台页面提取：

```powershell
.\.venv\Scripts\python -m pip install -r requirements-browser-debug.txt
```

### 5. 启动服务

```powershell
.\.venv\Scripts\python run_app.py
```

启动后打开：

```text
http://127.0.0.1:8765
```

## 使用指南

### 文件转写

1. 打开 Web UI。
2. 上传音频/视频文件，或先将文件放入 `input/` 目录后在页面中选择。
3. 选择 ASR 引擎、模型、语言、转写模式、输出格式和 CPU 参数。
4. 如有专业词汇，先在术语库中维护标准写法和别名。
5. 点击开始转写，页面会通过 SSE 实时显示任务进度。
6. 转写完成后下载 `TXT`、`SRT`、`VTT`、`TSV`、`CSV` 或 `JSON` 结果。

支持的输入扩展名：

```text
.mp3 .m4a .wav .flac .aac .ogg .mp4 .mkv
```

### 系统播放录音

系统播放模式适合无法直接下载媒体源、直播回放、播放器加密较多或网页探测失败的场景。

使用方式：

1. 安装 `requirements-capture-windows.txt`。
2. 在 Web UI 中切换到“系统播放”。
3. 选择默认 WASAPI loopback 设备或指定播放设备。
4. 点击开始采集，然后播放目标音频或视频。
5. 程序会录制系统声音，并每 20 秒提交一个隐藏转写任务。
6. 停止采集后，页面会继续收集剩余切片结果，直到实时转写完成。

录音文件会保存在：

```text
input/captures/
```

### 调试浏览器媒体探测

调试浏览器模式适合网页视频、网页音频、公开课程页面和平台视频页面。

使用方式：

1. 安装 `requirements-browser-debug.txt`。
2. 在 Web UI 中切换到“调试浏览器”。
3. 点击启动调试浏览器，程序会尝试启动 Chrome 或 Edge，并监听 `http://127.0.0.1:9222`。
4. 在新打开的浏览器里访问目标网页并播放媒体。
5. 回到 Easy-ASR，刷新标签页列表，选择目标标签页。
6. 点击监听媒体。程序会收集页面媒体元素、performance 资源和网络响应，并用 ffprobe 验证候选 URL。
7. 选择推荐候选后，可以直接转写，也可以下载音频或视频。

限制说明：

- 只支持 `http/https` 媒体 URL。
- `blob:`、`data:` 这类地址不能直接转写，需要从网络请求里找到真实媒体分片。
- DRM、Widevine、license、key、token 等控制端点不会被当作可转写媒体源。
- 受版权、登录态、地域、站点策略影响，部分页面只能通过系统播放录音处理。

### 命令行批处理

仓库保留了一个简单的 FunASR 批处理脚本：

```powershell
.\.venv\Scripts\python batch_transcribe.py
```

该脚本会扫描 `input/` 下的音频/视频文件，使用 FunASR SenseVoiceSmall CPU 模式转写，并将结果写入 `output/`。它适合作为最小批处理入口；更完整的引擎选择、导出格式和浏览器/录音能力请使用 Web UI。

## ASR 引擎说明

| 引擎 | 默认模型 | 适合场景 | 说明 |
| --- | --- | --- | --- |
| FunASR SenseVoiceSmall | `iic/SenseVoiceSmall` | 中文、多语种、课程和会议 | CPU 可用，内置 VAD、ITN、富文本后处理。当前术语库通过后处理替换增强。 |
| faster-whisper | `small` | Whisper 工作流、英文/多语种、句级时间戳 | CTranslate2 推理，CPU int8 友好，支持 tiny/base/small/medium/large-v3，术语库通过 `initial_prompt` 注入。 |

FunASR 还提供 `iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch` 作为可选模型。faster-whisper 支持填写内置短名、完整 HuggingFace repo id 或本地模型目录。

模型缓存默认放在项目目录下：

```text
models/modelscope/
models/hf/
models/torch/
models/faster-whisper/
```

如果 HuggingFace 下载不稳定，可以设置镜像端点：

```powershell
$env:EASY_ASR_HF_ENDPOINTS="https://hf-mirror.com,https://huggingface.co"
```

## 转写参数

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `language` | `zh` | 语言代码。可使用 `auto` 让引擎自动判断。 |
| `chunk_seconds` | `600` | 文件切片长度，范围限制为 30 到 3600 秒。 |
| `batch_size_s` | `60` | FunASR 推理批大小，范围限制为 10 到 300 秒。 |
| `merge_length_s` | `15` | FunASR VAD 合并长度，范围限制为 5 到 60 秒。 |
| `cpu_threads` | `4` | faster-whisper CPU 线程数，范围限制为 1 到 32。 |
| `compute_type` | `int8` | faster-whisper CTranslate2 计算类型。 |
| `whisper_preset` | `balanced` | faster-whisper 预设：`fast`、`balanced`、`quality`，对应不同 `beam_size`。 |
| `transcript_mode` | `whole` | 输出分段模式：整体文本、按切片、按句子。 |
| `apply_terminology` | `true` | 是否启用术语库增强。 |

## 术语库

术语库文件默认位于：

```text
data/terminology/default.json
```

Web UI 中可以直接编辑术语 JSON。每条术语格式如下：

```json
{
  "canonical": "随机梯度下降",
  "aliases": ["SGD", "stochastic gradient descent"],
  "weight": 1.0,
  "case_sensitive": false,
  "note": "机器学习术语"
}
```

字段说明：

| 字段 | 说明 |
| --- | --- |
| `canonical` | 希望最终保留的标准写法。 |
| `aliases` | 可能被识别出来的别名、英文缩写、错误写法或同义词。 |
| `weight` | 预留权重字段，便于后续扩展排序或匹配策略。 |
| `case_sensitive` | 是否区分大小写。 |
| `note` | 备注，方便团队维护术语来源和语境。 |

## 导出格式

| 格式 | 用途 |
| --- | --- |
| `TXT` | 纯文本稿，适合阅读、复制和二次编辑。 |
| `SRT` | 常见字幕格式，时间戳形如 `00:00:01,230 --> 00:00:04,560`。 |
| `VTT` | WebVTT 字幕格式，适合 HTML5 播放器和网页发布。 |
| `TSV` | 制表符分隔表格，包含 `start_ms`、`end_ms`、`text`。 |
| `CSV` | Excel 友好的表格格式，使用 UTF-8 BOM 写入，包含序号、时间和文本。 |
| `JSON` | 完整结构化结果，保留 source、engine、language、duration_seconds 和 segments。 |

任务输出默认位于：

```text
output/jobs/<源文件名>_<时间戳>_<任务ID>/
```

每个任务目录还会保存 `_job.json`，用于历史任务恢复和调试。

## 目录结构

```text
Easy-ASR/
|-- run_app.py                         # Web 服务启动入口，兼容源码和 PyInstaller 打包运行
|-- batch_transcribe.py                # 简单命令行批处理入口
|-- Start.bat                          # 打包版本启动脚本
|-- Easy-ASR.spec                      # PyInstaller 打包配置
|-- requirements.txt                   # FastAPI / Uvicorn / multipart 基础依赖
|-- requirements-asr-funasr.txt        # FunASR 引擎依赖
|-- requirements-asr-faster-whisper.txt # faster-whisper 引擎依赖
|-- requirements-capture-windows.txt   # Windows 系统播放采集依赖
|-- requirements-browser-debug.txt     # 调试浏览器和 yt-dlp 依赖
|-- easy_asr/
|   |-- main.py                        # FastAPI API 和静态资源入口
|   |-- jobs.py                        # 任务队列、状态、历史记录和输出目录管理
|   |-- audio.py                       # ffmpeg / ffprobe 音频切片和时长探测
|   |-- output.py                      # TXT / SRT / VTT / TSV / CSV / JSON 写出
|   |-- terminology.py                 # 术语库模型、加载、保存和文本增强
|   |-- capture.py                     # Windows WASAPI loopback 系统声采集
|   |-- browser_debug.py               # CDP 媒体探测、ffmpeg 提取、yt-dlp 兜底
|   |-- segmentation.py                # 句级分段辅助逻辑
|   |-- ytdlp_worker.py                # 打包环境中的 yt-dlp 子进程工作器
|   |-- debug_runtime.py               # 运行日志、崩溃日志和环境快照
|   `-- engines/
|       |-- base.py                    # ASR 引擎协议、参数和结果结构
|       |-- registry.py                # 引擎注册表
|       |-- funasr_engine.py           # FunASR SenseVoiceSmall / Paraformer 适配
|       `-- faster_whisper_engine.py   # faster-whisper 适配
|-- static/
|   |-- index.html                     # Web UI
|   |-- styles.css                     # 页面样式
|   |-- app.js                         # 前端交互逻辑
|   |-- logo.png
|   `-- logo-mark.png
|-- data/terminology/default.json      # 默认术语库
|-- input/                             # 输入文件、录音和浏览器媒体缓存
|-- output/                            # 转写结果
|-- chunks/                            # 临时切片目录
|-- models/                            # 本地模型缓存
`-- package_logs/                      # 运行日志和崩溃日志
```

## API 概览

Easy-ASR 的前端通过本地 REST API 和 SSE 与后端通信。常用接口包括：

| 接口 | 方法 | 说明 |
| --- | --- | --- |
| `/api/health` | `GET` | 服务健康检查和目录信息。 |
| `/api/models` | `GET` | 获取可用 ASR 引擎和模型选项。 |
| `/api/models/preload` | `POST` | 预下载/预加载模型。 |
| `/api/files` | `GET` | 列出 `input/` 目录中的输入文件。 |
| `/api/jobs` | `GET` / `POST` | 列出任务或创建文件转写任务。 |
| `/api/jobs/{job_id}` | `GET` | 获取任务详情。 |
| `/api/jobs/{job_id}/events` | `GET` | 任务进度 SSE。 |
| `/api/jobs/{job_id}/download/{format}` | `GET` | 下载指定格式输出。 |
| `/api/terminology` | `GET` / `PUT` | 读取或更新术语库。 |
| `/api/capture/devices` | `GET` | 枚举系统播放采集设备。 |
| `/api/capture/start` | `POST` | 开始系统播放录音。 |
| `/api/capture/{session_id}/stop` | `POST` | 停止系统播放录音。 |
| `/api/browser/launch` | `POST` | 启动调试浏览器。 |
| `/api/browser/tabs` | `GET` | 列出调试浏览器标签页。 |
| `/api/browser/probe` | `POST` | 探测并验证页面媒体候选。 |
| `/api/browser/transcribe` | `POST` | 提取浏览器媒体并提交转写。 |
| `/api/browser/download` | `POST` | 提取并下载浏览器媒体。 |

## 打包运行

仓库包含 `Easy-ASR.spec`，可用于 PyInstaller onedir 打包。打包后推荐目录形态如下：

```text
dist/Easy-ASR/
|-- Easy-ASR.exe
|-- Start.bat
|-- _internal/
|-- bin/
|   |-- ffmpeg.exe
|   `-- ffprobe.exe
|-- input/
|-- output/
|-- models/
`-- package_logs/
```

源码入口 `run_app.py` 会在打包模式下自动切换运行目录到 exe 所在目录，并将 `input/`、`output/`、`chunks/`、`models/`、`data/` 作为可写运行目录。启动失败或后台线程崩溃时，日志会写入：

```text
package_logs/runtime_*.log
package_logs/fatal_*.log
package_logs/crash_*.log
```

## 常见问题

### 启动后提示找不到 ffmpeg 或 ffprobe

源码运行时请将 ffmpeg 加入系统 `PATH`。打包运行时请确认 `bin/ffmpeg.exe` 和 `bin/ffprobe.exe` 位于 exe 同级目录、`_internal/bin/` 或 `vendor/ffmpeg/bin/`。

### 模型下载失败

可以重试模型预下载，或设置 `EASY_ASR_HF_ENDPOINTS` / `HF_ENDPOINT` 使用镜像。FunASR 会优先尝试 ModelScope，本地找不到时再尝试 HuggingFace 相关候选。

### 系统播放录音不可用

请确认已安装：

```powershell
.\.venv\Scripts\python -m pip install -r requirements-capture-windows.txt
```

并确认系统当前有可用的 WASAPI loopback 设备。该功能主要面向 Windows。

### 调试浏览器没有发现媒体源

可以按以下顺序排查：

1. 在调试浏览器中先播放目标视频或音频。
2. 勾选“刷新页面后监听”，并将监听时间调到 10 到 15 秒。
3. 如果页面使用 `blob:`，优先寻找 HLS/DASH/MP4/M4A 等真实网络请求候选。
4. 如果是支持的公开视频平台，尝试 `yt-dlp` 候选。
5. 如果仍然失败，回退到系统播放录音。

### SRT/VTT 时间戳不够细

选择 `sentence` 分段模式会尽量生成句级片段。不同引擎和模型的时间戳能力不同：faster-whisper 支持 word timestamp；FunASR 会尝试 sentence timestamp，不支持时会自动回退到文本拆句或切片时间。

## 开发说明

本项目没有引入复杂前端构建链，Web UI 直接由 `static/` 下的 HTML/CSS/JS 提供。后端启动后会将静态资源挂载到 `/static`，首页由 `static/index.html` 提供。

本地开发常用命令：

```powershell
.\.venv\Scripts\python run_app.py
```

建议提交前至少做一次基础启动验证：

```powershell
.\.venv\Scripts\python -m compileall easy_asr run_app.py batch_transcribe.py
```

## 路线图

- GPU 推理配置和显存策略。
- 更完善的批量任务管理和取消任务能力。
- 更细的术语库匹配策略和权重使用。
- 更多浏览器媒体站点策略。
- README 截图、演示 GIF 和发布包下载说明。

## 许可证

MIT License
