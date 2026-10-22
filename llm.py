"""DeepSeek 调用（只用标准库）。写法照 xiaogu-launch/web/agent.py 的 DeepSeekProvider。

key 读取顺序：环境变量 DEEPSEEK_API_KEY → 钥匙串条目 FITNESS_QA_KEYCHAIN（默认 xiaogu-deepseek）。
"""

from __future__ import annotations  # 让 /usr/bin/python3（3.9）也能跑 X | None 这类注解

import http.client
import json
import os
import subprocess
import urllib.error
import urllib.request

BASE_URL = os.environ.get("FITNESS_QA_BASE_URL", "https://api.deepseek.com").rstrip("/")
MODEL = os.environ.get("FITNESS_QA_MODEL", "deepseek-flash")


class LLMError(RuntimeError):
    pass


def _key() -> str:
    key = os.environ.get("DEEPSEEK_API_KEY")
    if key:
        return key
    service = os.environ.get("FITNESS_QA_KEYCHAIN", "xiaogu-deepseek")
    try:
        result = subprocess.run(["security", "find-generic-password", "-s", service, "-w"],
                                capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        result = None
    key = result.stdout.strip() if result else ""
    if not key:
        raise LLMError(f"未找到 DeepSeek key（环境变量 DEEPSEEK_API_KEY，或钥匙串条目 {service}）")
    return key


def _request(body: dict, timeout: int) -> urllib.request.Request:
    # thinking 默认开启时推理会吃掉 max_tokens、content 为空（2026-10-03 实测），统一显式关闭
    body = {"model": MODEL, "thinking": {"type": "disabled"}, **body}
    return urllib.request.Request(
        f"{BASE_URL}/chat/completions", data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {_key()}"},
    )


def chat_json(messages: list[dict], max_tokens: int = 2000, timeout: int = 120) -> tuple[dict, dict]:
    """JSON 模式调用，返回 (解析后的对象, usage)。提示词里必须出现 "json" 字样，否则接口报错。"""
    req = _request({"messages": messages, "response_format": {"type": "json_object"},
                    "max_tokens": max_tokens, "temperature": 0.2}, timeout)
    last = ""
    for _ in range(2):  # 传输层偶发断流（IncompleteRead 等）重试一次；HTTP 错误码不重试
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.load(resp)
            break
        except urllib.error.HTTPError as exc:
            raise LLMError(f"DeepSeek HTTP {exc.code}: {exc.read()[:200].decode('utf-8', 'replace')}") from None
        except (urllib.error.URLError, TimeoutError, http.client.HTTPException, OSError, ValueError) as exc:
            last = type(exc).__name__
    else:
        raise LLMError(f"DeepSeek 连接失败：{last}")
    content = data["choices"][0]["message"].get("content") or ""
    try:
        return json.loads(content), data.get("usage") or {}
    except json.JSONDecodeError:
        raise LLMError(f"模型没返回合法 JSON：{content[:200]}") from None


def chat_stream(messages: list[dict], max_tokens: int = 2500, timeout: int = 120):
    """流式调用，逐段 yield 文本，方便命令行边生成边显示。"""
    req = _request({"messages": messages, "stream": True, "max_tokens": max_tokens, "temperature": 0.3}, timeout)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        raise LLMError(f"DeepSeek HTTP {exc.code}: {exc.read()[:200].decode('utf-8', 'replace')}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise LLMError(f"DeepSeek 连接失败：{type(exc).__name__}") from None
    with resp:
        # SSE：每行 "data: {...}"，以 "data: [DONE]" 结束
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                delta = json.loads(payload)["choices"][0]["delta"].get("content")
            except (json.JSONDecodeError, KeyError, IndexError):
                continue
            if delta:
                yield delta
