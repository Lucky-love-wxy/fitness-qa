#!/usr/bin/env python3
"""健身知识问答：在 build.py 生成的索引上检索，交 DeepSeek 带出处作答。

检索逻辑：
1. 查询改写：DeepSeek 结合最近几轮对话，把问题改写成独立问句 + 3-5 组检索关键词（补同义说法，如「手腕」「护腕」「塌腕」）。
2. 双路 BM25（字二元组切词，只用标准库）：
   - 知识卡：每条视频一张，模型读过全文后重写的干净文本，不受转写错字影响，负责「找对视频」；
   - 原文块：逐字稿约 500 字一块、带起始时间，负责补细节和时间戳。
3. RRF 融合：每组关键词各排一次，按 1/(60+名次) 累加到视频和原文块上，取前 TOP_VIDEOS 条视频。
4. 作答：每条视频给摘要 + 带时间戳的要点 + 命中最高的原文块，要求每条结论标 [V编号 mm:ss]，库里没有就说没有。

用法：
  python3 ask.py                 多轮对话（/new 清空上下文，/src 显示检索命中，/q 退出）
  python3 ask.py -q "问题" [--src] 单次问答
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import defaultdict
from pathlib import Path

import llm

ROOT = Path(__file__).resolve().parent
INDEX_FILE = ROOT / "data" / "index.json"

TOP_VIDEOS = 6        # 交给模型的视频条数
CHUNKS_PER_VIDEO = 2  # 每条视频附带的原文块数
RRF_K = 60
HISTORY_TURNS = 3     # 改写和作答时带上最近几轮

CJK_RUN_RE = re.compile(r"[一-鿿]+")
WORD_RE = re.compile(r"[a-z0-9.]+")


def tokenize(text: str) -> list[str]:
    """中文按字二元组切（不依赖分词库，对转写错字也更宽容），英文数字按整词（5x5、rpe、100kg）。"""
    text = text.lower()
    tokens = []
    for run in CJK_RUN_RE.findall(text):
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[i:i + 2] for i in range(len(run) - 1))
    tokens.extend(w.strip(".") for w in WORD_RE.findall(text) if w.strip("."))
    return tokens


class BM25:
    """倒排索引版 BM25：建索引 O(总词数)，查询只遍历查询词的倒排表。"""

    def __init__(self, docs: list[str], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.postings: dict[str, list[tuple[int, int]]] = defaultdict(list)
        self.lengths = []
        for i, doc in enumerate(docs):
            toks = tokenize(doc)
            self.lengths.append(len(toks))
            tf: dict[str, int] = defaultdict(int)
            for t in toks:
                tf[t] += 1
            for t, c in tf.items():
                self.postings[t].append((i, c))
        self.n = len(docs)
        self.avgdl = sum(self.lengths) / max(self.n, 1)

    def search(self, query: str, top: int) -> list[int]:
        scores: dict[int, float] = defaultdict(float)
        for t in set(tokenize(query)):
            plist = self.postings.get(t)
            if not plist:
                continue
            idf = math.log(1 + (self.n - len(plist) + 0.5) / (len(plist) + 0.5))
            for i, c in plist:
                norm = c + self.k1 * (1 - self.b + self.b * self.lengths[i] / self.avgdl)
                scores[i] += idf * c * (self.k1 + 1) / norm
        return sorted(scores, key=scores.get, reverse=True)[:top]


class KnowledgeBase:
    def __init__(self, path: Path = INDEX_FILE):
        if not path.exists():
            sys.exit(f"没有索引：{path}。先跑 python3 {ROOT / 'build.py'}")
        data = json.loads(path.read_text(encoding="utf-8"))
        self.videos, self.chunks = data["videos"], data["chunks"]
        # 卡片文本里标题和关键词各重复一次，提高它们的权重
        card_docs = [" ".join([v["title"]] * 2 + [v["summary"]] + v["questions"] + v["keywords"] * 2
                              + [p.get("text", "") for p in v["points"]]) for v in self.videos]
        self.card_index = BM25(card_docs)
        self.chunk_index = BM25([c["text"] for c in self.chunks])

    def retrieve(self, queries: list[str]) -> list[dict]:
        video_score: dict[int, float] = defaultdict(float)
        chunk_score: dict[int, float] = defaultdict(float)
        for q in queries:
            for rank, vid in enumerate(self.card_index.search(q, 30)):
                video_score[vid] += 1 / (RRF_K + rank)
            seen = set()
            for rank, ci in enumerate(self.chunk_index.search(q, 60)):
                chunk_score[ci] += 1 / (RRF_K + rank)
                vid = self.chunks[ci]["vid"]
                if vid not in seen:  # 每组关键词下，视频只按它最好的原文块计一次，防止长视频刷分
                    seen.add(vid)
                    video_score[vid] += 1 / (RRF_K + rank)
        top = sorted(video_score, key=video_score.get, reverse=True)[:TOP_VIDEOS]
        hits = []
        for vid in top:
            mine = sorted((ci for ci in chunk_score if self.chunks[ci]["vid"] == vid),
                          key=chunk_score.get, reverse=True)[:CHUNKS_PER_VIDEO]
            hits.append({"video": self.videos[vid], "score": video_score[vid],
                         "chunks": [self.chunks[ci] for ci in sorted(mine, key=lambda c: self.chunks[c]["time"])]})
        return hits


REWRITE_PROMPT = """你负责把用户的健身问题改写成检索词。结合对话历史（追问时要补全指代），输出 json：
- question: 补全上下文后的独立问句
- queries: 3-5 组检索关键词字符串，每组 2-6 个词用空格分隔；覆盖不同说法和同义词（如 卧推/推胸、减脂/减肥/刷脂、碳水/主食、手腕/护腕/塌腕），包含相关动作名和术语
只输出 json。"""

ANSWER_PROMPT = """你是健身问答助手，只根据下面【资料】回答。资料来自一位健身博主的短视频（语音自动转写，有同音错字，引用时用正确的词）。

规则：
1. 每条结论后标出处，格式 [V编号 mm:ss]，时间取资料里对应要点或原文块的时间。
2. 观点表述为博主的观点；几条视频说法有出入时都列出并标注。
3. 资料没覆盖到的部分，直接说「视频库里没讲到」，不要用通用知识补；涉及疼痛、损伤、疾病时提醒线下就医。
4. 问题和健身无关、或资料都不相关时，只回答「视频库里没有相关内容」。
5. 先用一句话给结论，再分点展开，简洁，不用 emoji；输出在终端显示，不用 ** 加粗和 # 标题，分组用「一、二、」即可。"""


def format_materials(hits: list[dict]) -> str:
    blocks = []
    for n, h in enumerate(hits, 1):
        v = h["video"]
        lines = [f"[V{n}] 《{v['title']}》（主题：{v['topic']}）", f"摘要：{v['summary']}", "要点："]
        lines += [f"- {p.get('time', '')} {p.get('text', '')}" for p in v["points"]]
        for c in h["chunks"]:
            lines.append(f"原文块 {c['time']}：{c['text']}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def history_text(history: list[tuple[str, str]]) -> str:
    return "\n".join(f"用户：{q}\n助手：{a[:600]}" for q, a in history[-HISTORY_TURNS:])


def prepare(kb: KnowledgeBase, question: str, history: list[tuple[str, str]]) -> tuple[str, list[str], list[dict], list[dict]]:
    """改写 + 检索 + 拼作答消息，返回 (独立问句, 检索词, 命中, 作答 messages)。命令行和网页（server.py）共用。"""
    hist = history_text(history)
    rewritten, _ = llm.chat_json([{"role": "system", "content": REWRITE_PROMPT},
                                  {"role": "user", "content": f"对话历史：\n{hist or '（无）'}\n\n当前问题：{question}"}],
                                 max_tokens=400)
    standalone = rewritten.get("question") or question
    queries = [standalone] + [q for q in rewritten.get("queries", []) if isinstance(q, str)]
    hits = kb.retrieve(queries)
    messages = [{"role": "system", "content": ANSWER_PROMPT}]
    if hist:
        messages.append({"role": "user", "content": f"（之前的对话，供理解追问）\n{hist}"})
        messages.append({"role": "assistant", "content": "好的。"})
    messages.append({"role": "user", "content": f"【资料】\n{format_materials(hits)}\n\n【问题】{standalone}"})
    return standalone, queries, hits, messages


def answer(kb: KnowledgeBase, question: str, history: list[tuple[str, str]], show_src: bool = False) -> str:
    standalone, queries, hits, messages = prepare(kb, question, history)
    if show_src:
        print(f"\n· 改写：{standalone}\n· 关键词：{' | '.join(queries[1:])}")
        for n, h in enumerate(hits, 1):
            print(f"· V{n} {h['score']:.3f} 《{h['video']['title']}》 块 {[c['time'] for c in h['chunks']]}")
        print()

    out = []
    for piece in llm.chat_stream(messages):
        out.append(piece)
        print(piece, end="", flush=True)
    text = "".join(out)
    print()

    # 只列出答案里实际引用到的视频，给出文件夹路径方便打开对照
    cited = sorted({int(n) for n in re.findall(r"V(\d+)", text) if 0 < int(n) <= len(hits)})
    if cited:
        print("\n出处：")
        for n in cited:
            v = hits[n - 1]["video"]
            # 下载工具给的标题有张冠李戴（如不带后缀的「减脂土豆泥教程_」实际讲卧推，_(1) 才是土豆泥；已知的在 overrides.json 改了标题），附上按内容写的摘要开头便于辨认
            print(f"  V{n} 《{v['title']}》 内容：{v['summary'][:40]}…\n      {v['video'] or v['folder']}")
    return text


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("-q", "--question")
    ap.add_argument("--src", action="store_true", help="显示检索命中")
    args = ap.parse_args()
    kb = KnowledgeBase()

    if args.question:
        answer(kb, args.question, [], args.src)
        return

    try:
        import readline  # noqa: F401  让 input() 支持方向键和中文删除
    except ImportError:
        pass
    print(f"健身知识问答 · {len(kb.videos)} 条视频。/new 新话题  /src 显示检索命中  /q 退出")
    history: list[tuple[str, str]] = []
    show_src = args.src
    while True:
        try:
            q = input("\n问 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not q:
            continue
        if q in ("/q", "/quit", "exit"):
            break
        if q == "/new":
            history.clear()
            print("已清空上下文。")
            continue
        if q == "/src":
            show_src = not show_src
            print("显示检索命中：" + ("开" if show_src else "关"))
            continue
        try:
            history.append((q, answer(kb, q, history, show_src)))
        except llm.LLMError as exc:
            print(f"\n[出错] {exc}")


if __name__ == "__main__":
    main()
