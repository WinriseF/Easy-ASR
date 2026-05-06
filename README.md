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
```

### 为什么这样做

- CPU 优先：后台只开一个转写 worker，避免多个模型同时抢 CPU。
- 模型可替换：`easy_asr/engines/` 里每个模型是独立引擎，前端只依赖统一 API。
- 性能优先：模型在进程内缓存，任务之间不会重复加载；长音频先用 ffmpeg 切片，降低内存和失败成本。
- 术语增强：术语库会生成提示词给支持 prompt/hotword 的引擎，并对输出做确定性别名替换。SenseVoiceSmall 当前主要使用后处理；faster-whisper 会使用 `initial_prompt`。
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

## 目录

```text
easy_asr/
  main.py                  FastAPI 入口
  jobs.py                  任务队列和状态
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
output/jobs/
chunks/_jobs/
```

## 后续适配 GPU

后续只需要在引擎层增加 `device` 选项和对应的加载策略，不需要改前端主流程和任务 API。

