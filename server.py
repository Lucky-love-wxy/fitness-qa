#!/usr/bin/env python3
"""健身问答网页版的本机服务（只用标准库），检索和作答逻辑全部复用 ask.py。

接口：
- GET  /             index.html
- GET  /api/state    {"videos": 条数}，App 外壳用来探活
- POST /api/ask      {"question": "...", "history": [[问, 答], ...]} → text/event-stream：
                     event: meta  {"question", "queries", "sources": [{n, vid, title, topic, summary}]}
                     event: delta {"t": 文本片段}
                     event: done / error
- GET  /video/<vid>  视频文件，支持 Range（点出处时要能拖到指定秒数）

只监听 127.0.0.1；视频按 vid 提供，不把本地路径暴露给网页。端口 8775 已登记 ports-registry.json。
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import ask  # noqa: E402
import llm  # noqa: E402

PORT = int(os.environ.get("FITNESS_QA_PORT", "8775"))
RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")
KB = ask.KnowledgeBase()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # 静音默认访问日志，只留出错信息
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            body = (ROOT / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")  # 页面随时在改，不让 WKWebView 用旧版
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/state":
            self._json({"videos": len(KB.videos)})
        elif path.startswith("/video/"):
            self._video(path[len("/video/"):])
        else:
            self._json({"error": "not found"}, 404)

    def _video(self, vid_str: str):
        if not vid_str.isdigit() or int(vid_str) >= len(KB.videos):
            return self._json({"error": "no such video"}, 404)
        file = Path(KB.videos[int(vid_str)]["video"] or "")
        if not file.is_file():
            return self._json({"error": "video file missing"}, 404)
        size = file.stat().st_size
        start, end = 0, size - 1
        m = RANGE_RE.match(self.headers.get("Range", ""))
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else size - 1
            else:  # bytes=-N：最后 N 字节
                start = max(0, size - int(m.group(2)))
            end = min(end, size - 1)
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(file.name)[0] or "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        with file.open("rb") as f:
            f.seek(start)
            left = end - start + 1
            try:
                while left > 0:
                    buf = f.read(min(256 * 1024, left))
                    if not buf:
                        break
                    self.wfile.write(buf)
                    left -= len(buf)
            except (BrokenPipeError, ConnectionResetError):
                pass  # 播放器拖动进度时会主动断开旧请求

    def do_POST(self):
        if self.path != "/api/ask":
            return self._json({"error": "not found"}, 404)
        try:
            req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        except json.JSONDecodeError:
            return self._json({"error": "bad json"}, 400)
        question = str(req.get("question", "")).strip()
        if not question:
            return self._json({"error": "empty question"}, 400)
        history = [(str(q), str(a)) for q, a in req.get("history", []) if q and a]

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

        def emit(event: str, data: dict):
            self.wfile.write(f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8"))
            self.wfile.flush()

        try:
            standalone, queries, hits, messages = ask.prepare(KB, question, history)
            emit("meta", {"question": standalone, "queries": queries[1:], "sources": [
                {"n": n, "vid": h["video"]["vid"], "title": h["video"]["title"], "topic": h["video"]["topic"],
                 "summary": h["video"]["summary"], "has_video": bool(h["video"]["video"])}
                for n, h in enumerate(hits, 1)]})
            for piece in llm.chat_stream(messages):
                emit("delta", {"t": piece})
            emit("done", {})
        except llm.LLMError as exc:
            emit("error", {"message": str(exc)})
        except (BrokenPipeError, ConnectionResetError):
            pass  # 用户中途点了停止或关了窗口


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"健身问答：http://127.0.0.1:{PORT}/  （{len(KB.videos)} 条视频）", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
