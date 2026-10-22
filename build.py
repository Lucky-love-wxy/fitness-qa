#!/usr/bin/env python3
"""从 video-transcribe/out 的逐字稿里筛出健身内容，生成知识卡 + 原文块索引。

流程：
1. 规则预筛：剔掉明显非健身的来源（生姜 AI 实战、小红书求职、标题带 # 的视频号商业内容）。
2. 每篇全文交 DeepSeek 读一遍，出 JSON 知识卡（是否健身 / 主题 / 摘要 / 带时间戳的要点 / 本视频回答了哪些问题）。
   转写有同音错字，卡片是模型读懂后重写的干净文本，检索主要靠它。结果按「逐字稿大小+修改时间」缓存，重跑只处理新增或改动的。
3. 正文相似度去重（out/ 里有 _(1) 副本和同内容不同标题的重复下载）。
4. 原文按 [mm:ss] 段合并成约 500 字的块，保留起始时间，回答时用来补细节和定位。
5. 写 data/index.json（ask.py 读）和 data/excluded.md（剔除名单，给人审）。

用法：python3 build.py [--out <逐字稿目录>] [-w 并发数] [--limit N]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import llm

ROOT = Path(__file__).resolve().parent
# 逐字稿目录：环境变量 FITNESS_QA_TRANSCRIPTS > 仓库内 transcribe/out > 同级的 video-transcribe/out
DEFAULT_OUT = Path(os.environ.get("FITNESS_QA_TRANSCRIPTS") or next(
    (p for p in (ROOT / "transcribe" / "out", ROOT.parent / "video-transcribe" / "out") if p.is_dir()),
    ROOT / "transcribe" / "out"))
CARDS_CACHE = ROOT / "data" / "cards.json"
INDEX_FILE = ROOT / "data" / "index.json"
EXCLUDED_FILE = ROOT / "data" / "excluded.md"
OVERRIDES_FILE = ROOT / "data" / "overrides.json"  # 人工修正：include / exclude / titles

CHUNK_CHARS = 500          # 原文块目标长度
MAX_TRANSCRIPT_CHARS = 24000  # 超长逐字稿截断，防止单次请求过大（目前最长约 1.2 万字）
TOPICS = ["训练计划与进步", "卧推与推类", "深蹲硬拉与腿臀", "背部与拉类", "肩与手臂", "核心与腹肌",
          "减脂饮食与碳水", "增肌饮食与营养", "恢复睡眠与疲劳", "损伤康复与体态", "心肺与有氧",
          "健身认知与心态", "其他健身"]

SEG_RE = re.compile(r"^\[(\d{1,3}:\d{2}(?::\d{2})?)\]\s*(.*)$")

CARD_PROMPT = f"""你是健身知识整理员。下面是一条短视频的语音转写逐字稿（whisper 自动转写，有同音错字，如「手蚕」可能是「手腕」），每段前有 [mm:ss] 时间戳。

先判断：这条视频的主体内容是不是健身相关知识（力量训练、训练计划、动作技术、减脂/增肌饮食与营养、恢复与睡眠、运动损伤与体态、有氧心肺、健身认知与心态）。纯生活杂谈、商业/AI/职场、读书推荐、与健身无关的段子都不算。

然后输出 json，字段：
- is_fitness: true/false
- reason: 一句话说明判断依据
- topic: 从 {TOPICS} 里选一个最贴切的；非健身填 "非健身"
- summary: 120 字以内，说清这条视频的核心观点（用正确的词，修正转写错字）
- points: 数组，3-8 条具体知识点，每条 {{"text": "完整的一句结论或做法，带具体数字/动作名", "time": "该观点出现处的 mm:ss"}}
- questions: 数组，3-6 个这条视频能回答的、用户会怎么问的自然问句
- keywords: 数组，5-12 个检索关键词（动作名、部位、术语、同义说法）

非健身视频 points/questions/keywords 可以给空数组。只输出 json。"""


def parse_segments(md_text: str) -> list[tuple[str, str]]:
    """逐字稿.md → [(mm:ss, 段落文字)]"""
    segs = []
    for line in md_text.splitlines():
        m = SEG_RE.match(line.strip())
        if m and m.group(2).strip():
            segs.append((m.group(1), m.group(2).strip()))
    return segs


def to_seconds(ts: str) -> int | None:
    """mm:ss 或 h:mm:ss → 秒；格式不对返回 None"""
    try:
        parts = [int(x) for x in ts.strip().split(":")]
    except ValueError:
        return None
    sec = 0
    for x in parts:
        sec = sec * 60 + x
    return sec if 2 <= len(parts) <= 3 else None


def make_chunks(segs: list[tuple[str, str]]) -> list[dict]:
    """把相邻段合并到约 CHUNK_CHARS 字，块以第一段的时间戳为起点。"""
    chunks, buf, start = [], "", None
    for ts, text in segs:
        if start is None:
            start = ts
        buf += text
        if len(buf) >= CHUNK_CHARS:
            chunks.append({"time": start, "text": buf})
            buf, start = "", None
    if buf:
        # 尾巴太短就并进上一块，避免出现只有一句话的块
        if chunks and len(buf) < CHUNK_CHARS // 3:
            chunks[-1]["text"] += buf
        else:
            chunks.append({"time": start, "text": buf})
    return chunks


def rule_excluded(src: str, name: str) -> str | None:
    """返回剔除原因；None 表示进入模型判断。"""
    if "生姜-AI实战" in src:
        return "来源：生姜 AI 实战"
    if "SocialGrabber" in src:
        return "来源：小红书求职内容"
    if "#" in name:
        return "标题带 # 话题标签（视频号商业/AI 内容）"
    return None


def shingles(text: str) -> set[int]:
    """5 字片段的哈希，抽样 1/8 保留，用于快速估算两篇正文的重合度。"""
    t = re.sub(r"[^\w]", "", text)
    out = set()
    for i in range(len(t) - 4):
        h = int(hashlib.md5(t[i:i + 5].encode()).hexdigest()[:8], 16)
        if h % 8 == 0:
            out.add(h)
    return out


def load_items(out_dir: Path) -> list[dict]:
    # .done.json 是 video-transcribe 的转写记录 {来源路径: {"name": 子目录名}}；没有就直接按子目录扫，方便接别的转写工具
    done_file = out_dir / ".done.json"
    if done_file.exists():
        done = json.loads(done_file.read_text(encoding="utf-8"))
    else:
        done = {d.name: {"name": d.name} for d in sorted(out_dir.iterdir()) if d.is_dir()}
    items = []
    for src, meta in done.items():
        folder = out_dir / meta["name"]
        md = folder / "逐字稿.md"
        if not md.exists():
            continue
        st = md.stat()
        video = next((p for p in folder.iterdir() if p.suffix.lower() in (".mp4", ".mov", ".mp3", ".m4a")), None)
        items.append({"id": meta["name"], "title": meta["name"].strip("_ ").replace("_", " ").strip(),
                      "src": src, "folder": str(folder), "video": str(video) if video else "",
                      "md": md, "sig": f"{st.st_size}:{int(st.st_mtime)}"})
    return items


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("-w", "--workers", type=int, default=8)
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 篇（调试用）")
    args = ap.parse_args()
    if not args.out.is_dir():
        sys.exit(f"找不到逐字稿目录：{args.out}（用 --out 或环境变量 FITNESS_QA_TRANSCRIPTS 指定）")

    items = load_items(args.out)
    cache = json.loads(CARDS_CACHE.read_text(encoding="utf-8")) if CARDS_CACHE.exists() else {}
    ov = json.loads(OVERRIDES_FILE.read_text(encoding="utf-8")) if OVERRIDES_FILE.exists() else {}
    include, exclude = set(ov.get("include", [])), set(ov.get("exclude", []))
    excluded: list[tuple[str, str]] = []
    todo, kept = [], []
    for it in items:
        if it["id"] in exclude:
            excluded.append((it["title"], "人工剔除（overrides.json）"))
            continue
        # include 里的跳过规则预筛，仍交模型出卡片
        reason = None if it["id"] in include else rule_excluded(it["src"], it["id"])
        if reason:
            excluded.append((it["title"], reason))
            continue
        kept.append(it)
        hit = cache.get(it["id"])
        if not (hit and hit.get("sig") == it["sig"]):
            todo.append(it)
    if args.limit:
        todo = todo[:args.limit]
    print(f"逐字稿 {len(items)} 篇，规则剔除 {len(excluded)}，待模型读 {len(todo)}（缓存命中 {len(kept) - len(todo)}）", flush=True)

    lock = threading.Lock()
    usage_total = {"prompt_tokens": 0, "completion_tokens": 0}

    def work(it: dict) -> None:
        segs = parse_segments(it["md"].read_text(encoding="utf-8"))
        body = "\n".join(f"[{ts}] {t}" for ts, t in segs)[:MAX_TRANSCRIPT_CHARS]
        card, usage = llm.chat_json([{"role": "system", "content": CARD_PROMPT},
                                     {"role": "user", "content": f"视频标题：{it['title']}\n\n{body}"}])
        card["sig"] = it["sig"]
        with lock:
            cache[it["id"]] = card
            for k in usage_total:
                usage_total[k] += int(usage.get(k) or 0)

    failed = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(work, it): it for it in todo}
        for n, fut in enumerate(as_completed(futures), 1):
            it = futures[fut]
            try:
                fut.result()
            except Exception as exc:  # 单篇失败不影响整批，下次重跑会补
                failed.append((it["title"], str(exc)))
            if n % 20 == 0 or n == len(todo):
                with lock:
                    CARDS_CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
                print(f"  {n}/{len(todo)}", flush=True)
    CARDS_CACHE.parent.mkdir(exist_ok=True)
    CARDS_CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    if todo:
        print(f"本次 token：输入 {usage_total['prompt_tokens']}，输出 {usage_total['completion_tokens']}")
    for title, err in failed:
        print(f"  失败：{title}：{err}", file=sys.stderr)

    # 模型判为非健身的进剔除名单
    fitness = []
    for it in kept:
        card = cache.get(it["id"])
        if not card or card.get("sig") != it["sig"]:
            continue  # 本次失败或 --limit 未处理
        if card.get("is_fitness") or it["id"] in include:
            fitness.append((it, card))
        else:
            excluded.append((it["title"], f"模型判断：{card.get('reason', '')}"))

    # 去重：重合度高的两篇只留正文更长的那篇
    sh = {it["id"]: shingles(it["md"].read_text(encoding="utf-8")) for it, _ in fitness}
    fitness.sort(key=lambda x: -len(sh[x[0]["id"]]))
    unique: list[tuple[dict, dict]] = []
    for it, card in fitness:
        a = sh[it["id"]]
        # 抽样片段太少（几百字的短视频）时重合度不可信，不参与去重
        dup_of = next((u for u, _ in unique
                       if len(a) >= 50 and len(a & sh[u["id"]]) / len(a | sh[u["id"]]) > 0.5), None)
        if dup_of:
            excluded.append((it["title"], f"重复：与「{dup_of['title']}」正文重合"))
        else:
            unique.append((it, card))

    videos, chunks = [], []
    for vid, (it, card) in enumerate(sorted(unique, key=lambda x: x[0]["title"])):
        segs = parse_segments(it["md"].read_text(encoding="utf-8"))
        starts = sorted(to_seconds(ts) for ts, _ in segs)
        points = []
        for p in card.get("points") or []:
            # 模型会在段与段之间插值出 01:20 这类逐字稿里没有的时间，对齐到所在段的起点，跳转时不会错过内容
            sec = to_seconds(str(p.get("time", "")))
            snapped = max((s for s in starts if s <= sec), default=0) if sec is not None else 0
            points.append({"text": p.get("text", ""), "time": f"{snapped // 60:02d}:{snapped % 60:02d}"})
        videos.append({"vid": vid, "title": ov.get("titles", {}).get(it["id"], it["title"]), "folder": it["folder"], "video": it["video"],
                       "topic": card.get("topic", ""), "summary": card.get("summary", ""),
                       "points": points, "questions": card.get("questions") or [],
                       "keywords": card.get("keywords") or []})
        for c in make_chunks(segs):
            chunks.append({"vid": vid, **c})
    INDEX_FILE.write_text(json.dumps({"videos": videos, "chunks": chunks}, ensure_ascii=False), encoding="utf-8")

    lines = [f"# 剔除名单（{len(excluded)} 篇）", "",
             "觉得哪篇不该剔，告诉 Claude 标题即可（会写进 data/overrides.json 的 include 再重建）。", ""]
    for title, reason in sorted(excluded, key=lambda x: (x[1][:4], x[0])):
        lines.append(f"- {title}：{reason}")
    EXCLUDED_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"入库健身视频 {len(videos)} 条、原文块 {len(chunks)} 个；剔除 {len(excluded)} 篇 → {EXCLUDED_FILE}")


if __name__ == "__main__":
    main()
