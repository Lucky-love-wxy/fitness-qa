#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
批量视频 → 简体中文逐字稿（本机离线：whisper.cpp large-v3-turbo + silero VAD + OpenCC t2s）

  python3 transcribe.py <视频或文件夹> [更多...] [-o 输出目录] [--lang zh|auto|en...] [--force] [--events]

--events：给「批量转逐字稿.app」用，额外输出以 @@ 开头的 JSON 行（start/progress/done/summary），
App 靠它画每个文件的状态和进度条；人看的输出不变。

每个视频在输出目录（默认脚本同目录的 out/）下得到一个文件夹 <文件名>/：
  封面.jpg       同目录现成封面图 > 视频号下载工具数据库里的封面 > 视频第 1 秒截帧
  <原文件名>     视频本身（APFS 克隆，不多占磁盘；源文件不动）
  逐字稿.md      按约 40 秒合段，每段带 [mm:ss]
  逐字稿.txt     纯文本逐字稿（无时间戳，方便直接复制）
  字幕.srt       时间戳精确到句
整批结束在输出目录根部写 _汇总.md：成功/跳过/失败各几个、失败原因。
旧版平铺在 out/ 根部的 <文件名>.md/.txt/.srt，跳过判重时会自动收进各自文件夹并补上封面和视频。

设计取舍（详见 notes/2026-10-20-批量视频转逐字稿.md）：
- whisper 对普通话可能输出繁体，所以 OpenCC t2s 是必做步骤，不靠 prompt 赌运气。
- 默认 -l zh：短视频用 auto 容易误判语言；外语视频请显式 --lang auto/en（输出原文，不翻译）。
- 必开 VAD：长静音会让 whisper 陷入重复同一句的循环（memory reference_whisper_cpp_vad_local）。
  VAD 默认只留 30ms 余量会吃掉句首字（实测「第二个」→「二个」），这里放宽到 400ms。
- 不加 --prompt：实测带「简体+标点」prompt 会把整段并成一句、时间戳作废。
  不加 prompt 时 turbo 段内多数带标点，半角统一改全角；VAD 切出的无标点短段，拼段时补「，」、段尾补「。」。
- 串行处理：whisper 已占满 GPU，并行没收益；单个失败只记录，不中断整批。
- 判重用 文件大小+mtime（存在 .done.json 里），不对几个 G 的视频算 md5。
"""
import argparse, json, os, re, shutil, signal, sqlite3, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUT = os.path.join(HERE, "out")
# 模型默认放脚本同目录的 models/（下载地址见 preflight 报错），环境变量可指到别处
WHISPER_MODEL = os.path.expanduser(os.environ.get("VT_WHISPER_MODEL", os.path.join(HERE, "models", "ggml-large-v3-turbo-q5_0.bin")))
VAD_MODEL = os.path.expanduser(os.environ.get("VT_VAD_MODEL", os.path.join(HERE, "models", "ggml-silero-v5.1.2.bin")))
VAD_PAD_MS = "400"  # 语音段前后留白；30ms 默认值会切掉句首字
# whisper 中文常吐半角标点，统一成全角；「.」只在汉字后才换成句号，避免误伤小数和英文
HALF_TO_FULL = {",": "，", "?": "？", "!": "！", ":": "：", ";": "；"}
PARA_SECONDS = 40  # md 每段大约多长：太短读着碎，太长时间戳定位不准
# 视频号下载工具（wx_channels_download）把每条视频的封面 URL 存在它目录下的 data.db；
# 视频默认下到 <工具目录>/downloads/，所以按视频位置往上一级找库，VT_WX_DB 可直接指定；找不到就跳过这一步
WX_DB = os.environ.get("VT_WX_DB")
IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp")
# 文件夹内的固定文件名：文件夹名已经是视频名，里面不再重复，方便一眼认出哪个是什么
MD, TXT, SRT, COVER = "逐字稿.md", "逐字稿.txt", "字幕.srt", "封面.jpg"
MEDIA_EXT = {".mp4", ".mov", ".m4v", ".mkv", ".avi", ".webm", ".flv", ".wmv", ".ts",
             ".mp3", ".m4a", ".wav", ".aac", ".flac", ".ogg", ".opus", ".aiff"}


EVENTS = False


def emit(**ev):
    """App 读这些行；flush 保证进度实时到达，而不是攒到缓冲区满。"""
    if EVENTS:
        print("@@" + json.dumps(ev, ensure_ascii=False), flush=True)


def fmt_ts(sec):
    sec = int(sec)
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def fmt_srt(sec):
    ms = int(round(sec * 1000))
    return f"{ms // 3600000:02d}:{ms % 3600000 // 60000:02d}:{ms % 60000 // 1000:02d},{ms % 1000:03d}"


def collect(paths):
    """展开参数里的文件夹（递归），只留音视频文件；保持参数顺序、去重。"""
    out, seen = [], set()
    for p in paths:
        p = os.path.abspath(os.path.expanduser(p))
        if os.path.isdir(p):
            files = []
            for root, dirs, names in os.walk(p):
                dirs[:] = [d for d in dirs if not d.startswith(".")]
                files += [os.path.join(root, n) for n in names
                          if not n.startswith(".") and os.path.splitext(n)[1].lower() in MEDIA_EXT]
            cands = sorted(files)
        elif os.path.isfile(p):
            cands = [p]
        else:
            print(f"  找不到：{p}")
            continue
        for f in cands:
            if f not in seen:
                seen.add(f)
                out.append(f)
    return out


def full_width_punct(text):
    out = []
    for i, ch in enumerate(text):
        prev = text[i - 1] if i else ""
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if ch in HALF_TO_FULL and not (prev.isdigit() and nxt.isdigit()):
            out.append(HALF_TO_FULL[ch])
        elif ch == "." and "\u4e00" <= prev <= "\u9fff":
            out.append("。")
        else:
            out.append(ch)
    return "".join(out)


def has_audio(src):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
                        "-of", "csv=p=0", src], capture_output=True, text=True)
    return bool(r.stdout.strip())


def to_simplified(text):
    """OpenCC t2s 只做字形转换，不改词汇（「軟體」→「软体」而不是「软件」），保持逐字。"""
    r = subprocess.run(["opencc", "-c", "t2s.json"], input=text, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("OpenCC 转简体失败：" + r.stderr.strip()[-200:])
    return r.stdout.rstrip("\n")


def run_whisper(src, lang, tmpdir):
    """返回 (segments, 检测到的语言)。segments = [{start, end, text}]，秒。
    whisper 用 Popen 逐行读 stderr：开了 -pp 会打印「progress = NN%」，转成 progress 事件给 App。"""
    if not has_audio(src):
        raise RuntimeError("这个文件没有音轨")
    wav = os.path.join(tmpdir, "a.wav")
    r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", src, "-vn", "-ar", "16000", "-ac", "1",
                        "-c:a", "pcm_s16le", wav], capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(wav):
        raise RuntimeError("抽音轨失败：" + r.stderr.strip()[-200:])
    base = os.path.join(tmpdir, "w")
    cmd = ["whisper-cli", "-m", WHISPER_MODEL, "-f", wav, "--vad", "-vm", VAD_MODEL, "-vp", VAD_PAD_MS,
           "-l", lang, "-oj", "-of", base, "-np", "-pp"]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    tail = []  # 只留最后几行 stderr，失败时给出原因
    for line in proc.stderr:
        if "progress =" in line:
            try:
                emit(ev="progress", src=src, pct=int(line.rsplit("=", 1)[1].strip().rstrip("%")))
            except ValueError:
                pass
        else:
            tail = (tail + [line.strip()])[-5:]
    proc.wait()
    try:
        with open(base + ".json", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        raise RuntimeError("whisper 失败：" + (" ".join(tail)[-300:] or "无输出"))
    segs = []
    for s in data.get("transcription", []):
        text = s.get("text", "").strip()
        if text:
            segs.append({"start": s["offsets"]["from"] / 1000, "end": s["offsets"]["to"] / 1000, "text": text})
    return segs, (data.get("result") or {}).get("language", lang)


def is_cjk_lang(lang):
    return lang in ("zh", "ja", "yue")


CJK_PUNCT = ("。", "？", "！", "，", "、", "；", "：", "…", "”", "」")
CJK_END = ("。", "？", "！", "…", "”", "」")


def join_text(a, b, cjk):
    # 中文段之间是 VAD 切出的停顿，前一段没标点就补逗号，否则拼成一长串读不了
    if cjk:
        return a + b if a.endswith(CJK_PUNCT) else a + "，" + b
    return a + " " + b


def close_para(text, cjk):
    return text + "。" if cjk and text and not text.endswith(CJK_END) else text


def to_paragraphs(segs, cjk):
    """合段：满 PARA_SECONDS 后在句末断开；一直没有句末标点就到 1.5 倍时长硬断。"""
    paras, cur = [], None
    for s in segs:
        if cur is None:
            cur = {"start": s["start"], "text": s["text"]}
        else:
            cur["text"] = join_text(cur["text"], s["text"], cjk)
        span = s["end"] - cur["start"]
        ends = cur["text"].rstrip().endswith((".", "?", "!", "。", "？", "！", "…"))
        if (span >= PARA_SECONDS and ends) or span >= PARA_SECONDS * 1.5:
            cur["text"] = close_para(cur["text"], cjk)
            paras.append(cur)
            cur = None
    if cur:
        cur["text"] = close_para(cur["text"], cjk)
        paras.append(cur)
    return paras


def write_outputs(out_dir, name, src, segs, lang, elapsed):
    cjk = is_cjk_lang(lang)
    paras = to_paragraphs(segs, cjk)
    dur = segs[-1]["end"] if segs else 0
    md = [f"# {name} — 逐字稿", "",
          f"- 源文件：{src}",
          f"- 语言：{lang}；转写：本机 whisper large-v3-turbo + VAD" + ("，OpenCC 转简体" if lang == "zh" else ""),
          f"- 时长约 {fmt_ts(dur)}，转写用时 {int(elapsed)} 秒", ""]
    for p in paras:
        md += [f"[{fmt_ts(p['start'])}] {p['text']}", ""]
    folder = os.path.join(out_dir, name)
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, MD), "w", encoding="utf-8") as f:
        f.write("\n".join(md))
    with open(os.path.join(folder, TXT), "w", encoding="utf-8") as f:
        f.write("\n\n".join(p["text"] for p in paras) + "\n")
    srt = []
    for i, s in enumerate(segs, 1):
        srt += [str(i), f"{fmt_srt(s['start'])} --> {fmt_srt(s['end'])}", s["text"], ""]
    with open(os.path.join(folder, SRT), "w", encoding="utf-8") as f:
        f.write("\n".join(srt))


def norm_title(s):
    """标题比对前去掉标点、空白、下划线和 .mp4：下载工具落盘时会截断、加「_」后缀，原样比对不上。"""
    return re.sub(r"[\W_]+", "", re.sub(r"\.mp4", "", s, flags=re.I))


def sibling_cover(src):
    """同目录现成的封面图：同名图片，或「<前缀>_封面.jpg」且前缀是视频名开头（小红书 SocialGrabber 的落盘方式）。"""
    d, stem = os.path.dirname(src), os.path.splitext(os.path.basename(src))[0]
    for n in sorted(os.listdir(d)):
        base, ext = os.path.splitext(n)
        if ext.lower() not in IMAGE_EXT:
            continue
        prefix = base[:-2].rstrip("_-") if base.endswith("封面") else ""
        if base == stem or (prefix and stem.startswith(prefix)):
            return os.path.join(d, n)
    return None


def wx_cover_url(src):
    """在视频号下载工具的库里按标题找封面 URL；先精确匹配，再退到前 20 个字的唯一前缀匹配（长标题文件名会被截断）。"""
    db_path = WX_DB or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(src))), "data.db")
    if not os.path.exists(db_path):
        return None
    try:
        db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        rows = db.execute("select name, cover_url from download_task where cover_url <> ''").fetchall()
        db.close()
    except sqlite3.Error:
        return None  # 工具正在写库或表结构变了：不影响转写，退回截帧
    key = norm_title(os.path.splitext(os.path.basename(src))[0])
    if not key:
        return None
    exact = [u for n, u in rows if norm_title(n) == key]
    if exact:
        return exact[0]
    pre = [u for n, u in rows if len(key) >= 20 and norm_title(n)[:20] == key[:20]]
    return pre[0] if len(set(pre)) == 1 else None


def make_cover(src, dst):
    """按「现成图 > 视频号库 > 截帧」顺序生成封面，返回来源说明；三种都失败返回 None（不算转写失败）。"""
    img = sibling_cover(src)
    if img:
        r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", img, dst], capture_output=True)
        if r.returncode == 0:
            return "同目录封面图"
    url = wx_cover_url(src)
    if url:
        # 用 curl 不用 urllib：brew 的 Python 没装根证书，urllib 访问 https 会证书校验失败
        try:
            got = subprocess.run(["curl", "-sfL", "--max-time", "20", "-o", dst + ".part", url],
                                 capture_output=True).returncode == 0
            # 视频号给的可能是 webp/jpg 混着来，统一用 ffmpeg 转一遍保证后缀名和内容一致
            if got and subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", dst + ".part", dst],
                                      capture_output=True).returncode == 0:
                return "视频号封面"
        finally:
            if os.path.exists(dst + ".part"):
                os.remove(dst + ".part")
    # 截第 1 秒而不是第 0 帧：很多视频开头是黑场或转场
    for t in ("1", "0"):
        r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", t, "-i", src, "-frames:v", "1", "-q:v", "2", dst],
                           capture_output=True)
        if r.returncode == 0 and os.path.exists(dst):
            return "视频截帧"
    return None


def clone_video(src, dst):
    """cp -c 走 APFS clonefile：不复制数据、不占额外空间；跨卷或不支持时退回普通复制。"""
    if subprocess.run(["cp", "-c", src, dst], capture_output=True).returncode != 0:
        shutil.copy2(src, dst)


def finish_folder(out_dir, name, src):
    """把一个视频的文件夹补齐：旧版平铺产物收进来、视频克隆进来、封面生成。已有的不重做。"""
    folder = os.path.join(out_dir, name)
    os.makedirs(folder, exist_ok=True)
    for ext, fixed in ((".md", MD), (".txt", TXT), (".srt", SRT)):
        old = os.path.join(out_dir, name + ext)
        if os.path.exists(old) and not os.path.exists(os.path.join(folder, fixed)):
            os.replace(old, os.path.join(folder, fixed))
    if os.path.exists(src):
        vid = os.path.join(folder, os.path.basename(src))
        if not os.path.exists(vid) and os.path.splitext(src)[1].lower() in MEDIA_EXT:
            clone_video(src, vid)
        cover = os.path.join(folder, COVER)
        if not os.path.exists(cover):
            make_cover(src, cover)


def out_name(src, used):
    """输出文件名用视频文件名；不同文件夹里同名的加 -2、-3 区分。"""
    base = os.path.splitext(os.path.basename(src))[0]
    name, i = base, 2
    while name in used:
        name, i = f"{base}-{i}", i + 1
    used.add(name)
    return name


def load_done(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_done(path, done):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(done, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def preflight():
    missing = [n for n in ("ffmpeg", "whisper-cli", "opencc") if not shutil.which(n)]
    missing += [p for p in (WHISPER_MODEL, VAD_MODEL) if not os.path.exists(p)]
    if missing:
        sys.exit("缺少依赖：\n  " + "\n  ".join(missing) + "\n（ffmpeg/whisper-cpp/opencc 用 brew install 装；模型下载：\n"
                 "  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo-q5_0.bin\n"
                 "  https://huggingface.co/ggml-org/whisper-vad/resolve/main/ggml-silero-v5.1.2.bin\n"
                 f"  放进 {os.path.join(HERE, 'models')}/，或用 VT_WHISPER_MODEL / VT_VAD_MODEL 指定）")


def main():
    ap = argparse.ArgumentParser(description="批量视频 → 简体中文逐字稿")
    ap.add_argument("paths", nargs="+", help="视频文件或文件夹（文件夹递归）")
    ap.add_argument("-o", "--out", default=DEFAULT_OUT, help="输出目录")
    ap.add_argument("--lang", default="zh", help="zh（默认）/ auto / en ... 外语输出原文，不翻译")
    ap.add_argument("--force", action="store_true", help="已转过的也重转")
    ap.add_argument("--events", action="store_true", help="输出 @@JSON 进度行（给 App 用）")
    a = ap.parse_args()
    global EVENTS
    EVENTS = a.events
    # App 点「停止」发 SIGTERM：转成正常退出，让 TemporaryDirectory 删掉临时 wav（长视频几百 MB）。
    # SystemExit 不是 Exception，不会被下面逐文件的 except 当成「转写失败」吞掉
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    preflight()

    files = collect(a.paths)
    if not files:
        sys.exit("没有找到音视频文件。")
    os.makedirs(a.out, exist_ok=True)
    done_path = os.path.join(a.out, ".done.json")
    done = load_done(done_path)
    # 之前批次已占用的输出名全部预留：否则另一个目录下的同名视频会拿到同一个名字，覆盖上一批的产物
    used = {v["name"] for v in done.values()}
    results = []  # (状态, 文件, 说明)
    print(f"共 {len(files)} 个文件，输出到 {a.out}\n", flush=True)

    for i, src in enumerate(files, 1):
        st = os.stat(src)
        sig = f"{st.st_size}:{int(st.st_mtime)}:{a.lang}"
        prev = done.get(src)
        # 判重：同一文件且没改过、上次的产物还在 → 跳过，名字沿用上次的
        # 旧版平铺的 <name>.md 也算转过，finish_folder 会把它收进文件夹
        if prev and prev.get("sig") == sig and not a.force \
                and (os.path.exists(os.path.join(a.out, prev["name"], MD))
                     or os.path.exists(os.path.join(a.out, prev["name"] + ".md"))):
            finish_folder(a.out, prev["name"], src)
            results.append(("跳过", src, prev["name"] + "/（之前已转，已补齐封面和视频）"))
            emit(ev="done", src=src, state="跳过", name=prev["name"], note="之前已转过")
            print(f"[{i}/{len(files)}] 跳过（已转过）{os.path.basename(src)}", flush=True)
            continue
        # 同一源文件重转沿用它自己上次的名字；新文件避开所有已占用的名字
        name = prev["name"] if prev else out_name(src, used)
        print(f"[{i}/{len(files)}] 转写 {os.path.basename(src)} ...", flush=True)
        emit(ev="start", src=src, name=name, i=i, n=len(files))
        t0 = time.time()
        try:
            with tempfile.TemporaryDirectory(prefix="vt-") as tmp:
                segs, lang = run_whisper(src, a.lang, tmp)
            if lang == "zh":
                # 整批一次性送 OpenCC，比每段起一个进程快；用不会出现在正文里的分隔行切回去
                sep = "\n␞\n"
                conv = to_simplified(sep.join(s["text"] for s in segs)).split(sep)
                if len(conv) != len(segs):
                    raise RuntimeError("OpenCC 输出段数对不上")
                for s, t in zip(segs, conv):
                    s["text"] = full_width_punct(t.strip())
            if not segs:
                # 实测纯配乐+画面字幕的视频会走到这里（不开 VAD 时 whisper 只会吐「字幕志愿者」之类幻觉句）
                raise RuntimeError("没检测到人声：多半是纯配乐、文字烧在画面上的视频，这类要 OCR，本工具不处理")
            elapsed = time.time() - t0
            write_outputs(a.out, name, src, segs, lang, elapsed)
            finish_folder(a.out, name, src)
            done[src] = {"sig": sig, "name": name}
            save_done(done_path, done)
            note = f"{name}/，{len(segs)} 句，用时 {int(elapsed)} 秒"
            if a.lang == "auto" and lang != "zh":
                note += f"，检测为 {lang}（原文，未翻译）"
            results.append(("成功", src, note))
            emit(ev="done", src=src, state="成功", name=name, note=note)
            print(f"    完成：{note}", flush=True)
        except Exception as e:
            results.append(("失败", src, str(e)))
            emit(ev="done", src=src, state="失败", name=name, note=str(e))
            print(f"    失败：{e}", flush=True)

    counts = {k: sum(1 for r in results if r[0] == k) for k in ("成功", "跳过", "失败")}
    summary = [f"# 本批转写汇总 {time.strftime('%Y-%m-%d %H:%M')}", "",
               f"成功 {counts['成功']}，跳过 {counts['跳过']}，失败 {counts['失败']}", ""]
    for state in ("失败", "成功", "跳过"):
        rows = [r for r in results if r[0] == state]
        if rows:
            summary += [f"## {state}", ""] + [f"- {os.path.basename(s)}：{n}" for _, s, n in rows] + [""]
    with open(os.path.join(a.out, "_汇总.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(summary))
    emit(ev="summary", out=a.out, **counts)
    print(f"\n成功 {counts['成功']}，跳过 {counts['跳过']}，失败 {counts['失败']}。汇总：{os.path.join(a.out, '_汇总.md')}")
    sys.exit(1 if counts["失败"] else 0)


if __name__ == "__main__":
    main()
