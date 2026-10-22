# 第 2 步：批量视频转逐字稿

一批视频（或音频）进去，每个出一份简体中文逐字稿。全程本机离线：whisper.cpp large-v3-turbo + silero VAD + OpenCC。

## 准备
```bash
brew install ffmpeg whisper-cpp opencc
mkdir -p transcribe/models
curl -L -o transcribe/models/ggml-large-v3-turbo-q5_0.bin https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo-q5_0.bin
curl -L -o transcribe/models/ggml-silero-v5.1.2.bin https://huggingface.co/ggml-org/whisper-vad/resolve/main/ggml-silero-v5.1.2.bin
```
模型已经在别处的，用环境变量 `VT_WHISPER_MODEL` / `VT_VAD_MODEL` 指过去即可。

## 用法
命令行（文件夹会递归）：
```bash
python3 transcribe/transcribe.py <视频或文件夹...> [-o 输出目录] [--lang zh|auto|en] [--force]
```

macOS App（可选，拖文件进窗口、看进度、预览逐字稿）：
```bash
transcribe/app/build.sh --install   # 装到 ~/Applications/批量转逐字稿.app
```
App 直接调用仓库里的 `transcribe.py`，装好后不要移动仓库，移动了就重跑一次。

## 产物（默认在 `transcribe/out/`）
每个视频一个文件夹 `out/<文件名>/`：
- `逐字稿.md`：约 40 秒一段，每段带 `[mm:ss]`，第 3 步建库直接读它
- `逐字稿.txt`：纯文本
- `字幕.srt`：句级字幕
- `封面.jpg`：同目录现成封面图 > 视频号下载工具 `data.db` 里的原封面 > 视频第 1 秒截帧
- 视频本身：APFS 克隆，不多占磁盘，源文件不动

`out/_汇总.md` 记录本批成功、跳过、失败及原因。已转过且没改动的文件自动跳过（按大小 + 修改时间判断），`--force` 强制重转。

## 几个实测过的取舍
- 必开 VAD：长静音会让 whisper 反复输出同一句。VAD 默认只留 30ms 余量会吃掉句首字，这里放宽到 400ms。
- 不加 `--prompt`：带「简体 + 标点」提示词会把整段并成一句，时间戳作废。繁简转换交给 OpenCC。
- 默认按普通话识别；外语视频加 `--lang auto` 或 `--lang en`，输出原文，不翻译。
- 纯配乐、字幕烧在画面上的视频没有人声，会报「没检测到人声」，这类要 OCR，本工具不处理。
- 速度参考（Apple M5）：9 分钟口播约 36 秒。
