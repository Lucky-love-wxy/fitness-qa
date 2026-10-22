# 健身问答（fitness-qa）

把一批健身短视频的语音逐字稿做成可问答的知识库：问一句，DeepSeek 只根据这些视频的内容回答，每条结论标 `[V编号 mm:ss]`，点出处从那一秒开始播放原视频。

- 只用 Python 标准库，不装依赖、不起容器、不需要向量库
- 逐字稿是语音转写，有同音错字：先让模型逐篇读一遍生成干净的「知识卡」，检索主要靠卡片，原文块用来补细节和定位时间
- 库里没讲的直接说没讲，不用通用知识补

仓库只有代码，不带任何视频或逐字稿，知识库要用你自己的资料生成。完整链路三步，每步都能单独用：

1. [下载视频号视频](wx-channels/README.md)：用开源工具 wx_channels_download（不附带，去上游下载）
2. [批量转逐字稿](transcribe/README.md)：本机离线 whisper.cpp，输出带时间戳的 `逐字稿.md`
3. 建库问答：本页下文

已经有逐字稿的，直接从第 3 步开始。

## 环境
- Python 3.9+
- DeepSeek API key：环境变量 `DEEPSEEK_API_KEY`（macOS 也可以存进钥匙串，见下文「配置」）
- 可选：macOS 13+ 和 Xcode 命令行工具，用来打包原生 App 外壳

## 准备逐字稿
一个目录，每条视频一个子文件夹：

```
transcripts/
├── 卧推肩膀疼怎么办/
│   ├── 逐字稿.md          # 必需
│   └── 卧推肩膀疼怎么办.mp4  # 可选（.mp4/.mov/.mp3/.m4a），有它才能点出处跳到对应秒数
└── 5x5力训计划/
    └── 逐字稿.md
```

`逐字稿.md` 每段一行，行首是时间戳，`mm:ss` 或 `h:mm:ss` 都行：

```
[00:00] 体重 90 公斤能做 100 公斤高位下拉，引体却拉不了几个……
[01:02] 健美式训练很吃天赋……
```

子文件夹名就是视频标题。第 2 步的 `transcribe/transcribe.py` 直接输出这个格式（默认在 `transcribe/out/`，`build.py` 不加 `--out` 时优先读它）；用别的转写工具，按这个格式落盘即可。

## 用法
```bash
git clone https://github.com/Lucky-love-wxy/fitness-qa.git
cd fitness-qa
export DEEPSEEK_API_KEY=你的key
cp data/overrides.example.json data/overrides.json   # 可选：人工修正收录 / 剔除 / 标题

# 1. 建库：规则预筛 → 模型逐篇出知识卡 → 去重 → 原文切块 → data/index.json
python3 build.py --out /path/to/transcripts

# 2a. 命令行问答（多轮；/new 换话题，/src 显示检索命中，/q 退出）
python3 ask.py
python3 ask.py -q "卧推肩膀疼怎么办" --src

# 2b. 网页版：浏览器打开 http://127.0.0.1:8775/
python3 server.py
```

新增或改动了逐字稿，重跑 `build.py` 即可，只处理变化的部分（按文件大小 + 修改时间缓存）。重建后要重启 `server.py`。

## macOS App（可选）
```bash
mac/build.sh --install   # 装到 ~/Applications/健身问答.app
```
App 启动时自己拉起 `server.py`，退出时关掉。它直接读本仓库目录，所以装好后不要移动仓库；移动了就重跑一次 `build.sh --install`。

## 配置
| 环境变量 | 默认值 | 作用 |
|---|---|---|
| `DEEPSEEK_API_KEY` | 无 | DeepSeek key；没设时在 macOS 上读钥匙串 |
| `FITNESS_QA_KEYCHAIN` | `xiaogu-deepseek` | 钥匙串条目名（`security add-generic-password -s <名字> -a deepseek -w` 存入） |
| `FITNESS_QA_MODEL` | `deepseek-flash` | 模型名 |
| `FITNESS_QA_BASE_URL` | `https://api.deepseek.com` | 换成其他 OpenAI 兼容接口 |
| `FITNESS_QA_TRANSCRIPTS` | `transcribe/out` | 逐字稿目录，等同 `build.py --out` |
| `FITNESS_QA_PORT` | `8775` | 网页版端口 |

## 文件
- `build.py`：建库。`rule_excluded()` 里是作者自己资料的预筛规则，换成你的资料时按需改
- `ask.py`：查询改写 → 知识卡 / 原文块双路 BM25（字二元组）→ RRF 融合 → 带出处作答
- `llm.py`：DeepSeek 调用（JSON 模式 + 流式）
- `server.py` + `index.html`：网页版，只监听 127.0.0.1；视频按编号提供，支持 Range 拖动
- `mac/`：原生外壳（Swift + WKWebView）
- `data/`：生成的知识库（不进仓库）。`excluded.md` 是剔除名单，可人工复核；`overrides.json` 做人工修正，改完重跑 `build.py`

## 边界
- 答案是「视频作者的观点」，不是医学或训练建议，伤病问题请找专业人士。
- 检索是关键词级，没有向量检索；问法和视频用词差距大时可能漏召回，用 `--src` / `/src` 看命中了什么。
- 请只对你有权使用的视频资料建库，生成的知识库不要公开分发他人内容。

## License
MIT
