# Easy-ASR

本地语音转写工作站 | Local Speech-to-Text Workstation

## 简介

Easy-ASR 是一款本地运行的语音转写工具，提供 Web 界面和命令行两种使用方式。支持多种音频输入源和 ASR 引擎，适合中文课程、访谈、会议等场景的语音转写。

## 功能特性

- **多引擎支持**：FunASR SenseVoiceSmall、faster-whisper
- **多输入源**：本地音频文件、系统播放录音、调试浏览器媒体探测
- **实时转写**：系统播放时边录边转，支持长音频切片处理
- **术语增强**：自定义术语库，提升专业名词识别准确率
- **多格式导出**：TXT、SRT、VTT、TSV、CSV、JSON
- **CPU 优化**：单 worker 队列，避免多模型抢 CPU

## 架构

```
Browser UI (Web 界面)
    │
    ├── FastAPI REST/SSE
    │       │
    │       ├── JobManager 单 worker 队列
    │       │       │
    │       │       ├── ASR Engine 插件
    │       │       │       ├── FunASR SenseVoiceSmall
    │       │       │       └── faster-whisper
    │       │       │
    │       │       ├── TerminologyLibrary 术语库增强
    │       │       │
    │       │       └── 导出: TXT/SRT/VTT/JSON
    │       │
    │       └── Audio 处理 (ffmpeg)
    │
    ├── System Playback (系统播放录音)
    │       │
    │       └── Windows WASAPI loopback capture
    │               │
    │               └── 边录边转写
    │
    └── Debug Browser (调试浏览器)
            │
            └── Chrome DevTools Protocol
                    │
                    └── 提取原始媒体 URL / HLS / DASH
```

## 快速开始

### 环境要求

- Python 3.10+
- ffmpeg / ffprobe
- Windows (用于系统播放录音和调试浏览器功能)

### 安装

```powershell
# 创建虚拟环境
python -m venv .venv

# 安装核心依赖
.\.venv\Scripts\python -m pip install -r requirements.txt

# 安装 ASR 引擎 (二选一)

# 推荐：FunASR SenseVoiceSmall (适合中文)
.\.venv\Scripts\python -m pip install -r requirements-asr-funasr.txt

# 或：faster-whisper
.\.venv\Scripts\python -m pip install -r requirements-asr-faster-whisper.txt
```

### 运行

```powershell
# 启动 Web 服务
.\.venv\Scripts\python run_app.py
```

打开浏览器访问：`http://127.0.0.1:8765`

## 使用方式

### 1. 文件转写

1. 将音频文件放入 `input/` 目录，或在页面直接上传
2. 选择 ASR 引擎（推荐 SenseVoiceSmall）
3. 如有需要，编辑术语库添加专业名词
4. 点击开始转写
5. 任务完成后下载结果

### 2. 系统播放录音

选择"系统播放"模式，选择播放设备后开始监听。录音过程中会实时分段追加转写文本。

### 3. 调试浏览器模式

适用于在线视频、音频网站的转写。

直接点击页面上的"启动调试浏览器"按钮，选择 Bing、百度或 Google 等搜索引擎，后端会自动打开调试浏览器。在打开的浏览器中访问目标网站后，回到 Easy-ASR 页面选择对应标签页，点击"监听媒体"，发现媒体源后点击"原源转写"。

注意：此模式不适用于 DRM 加密内容。

## 术语库格式

```json
{
  "canonical": "随机梯度下降",
  "aliases": ["SGD", "stochastic gradient descent"],
  "weight": 1.0,
  "case_sensitive": false,
  "note": "机器学习术语"
}
```

## 导出格式

| 格式 | 说明 |
|------|------|
| TXT | 纯文稿，适合阅读和复制 |
| SRT | 标准字幕格式，时间戳 `00:00:01,230 --> 00:00:04,560` |
| VTT | WebVTT 格式，适合 HTML5 播放器 |
| TSV | 分段表格，列为 `start_ms`、`end_ms`、`text` |
| CSV | 分段表格，含序号、起止时间、文本 |
| JSON | 完整结构化结果，保留引擎、语言、segments 等 |

## 命令行模式

```powershell
# 将音频放入 input/ 目录
.\.venv\Scripts\python batch_transcribe.py
```

## 项目结构

```
easy_asr/
├── main.py                  # FastAPI 入口
├── jobs.py                 # 任务队列和状态管理
├── capture.py             # Windows 系统播放声音采集
├── browser_debug.py       # 调试浏览器原始媒体源探测
├── audio.py              # ffmpeg/ffprobe 处理
├── terminology.py        # 术语库管理
├── engines/
│   ├── base.py          # 引擎基类和接口
│   ├── funasr_engine.py      # SenseVoiceSmall 引擎
│   ├── faster_whisper_engine.py
│   └── registry.py      # 引擎注册表
static/
├── index.html           # Web 界面
├── styles.css
└── app.js
data/terminology/
└── default.json       # 默认术语库
input/                # 输入音频目录
output/                # 输出结果目录
chunks/                # 临时切片目录
```

## 后续 GPU 支持

接口已预留 `device` 选项，后续只需在引擎层添加对应加载策略即可支持 GPU。

## 许可证

MIT License