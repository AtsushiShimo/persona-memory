"""Claude Code JSONL transcript の読み取り。

phase 2 ではテキスト + tool_use / tool_result を flatten して content を組み立てる。
これは「全発話を記憶」 の北極星に従い、tool 経由のデータも残すため。
"""
from __future__ import annotations

import json
from pathlib import Path


def _flatten_tool_result(body) -> str:
    if isinstance(body, str):
        return body
    if isinstance(body, list):
        parts: list[str] = []
        for c in body:
            if isinstance(c, dict) and c.get("type") == "text":
                parts.append(c.get("text", ""))
            elif isinstance(c, str):
                parts.append(c)
        return "\n".join(parts)
    return str(body)


def read_transcript(path: str) -> list[dict]:
    """JSONL を user/assistant の {role, content} のリストに変換。"""
    out: list[dict] = []
    p = Path(path)
    if not p.exists():
        return out
    try:
        with p.open(encoding="utf-8") as f:
            for line in f:
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if msg.get("type") not in ("user", "assistant"):
                    continue
                m = msg.get("message") or {}
                role = m.get("role", "")
                content = m.get("content", "")
                if isinstance(content, list):
                    parts: list[str] = []
                    for c in content:
                        if not isinstance(c, dict):
                            continue
                        ctype = c.get("type")
                        if ctype == "text":
                            t = c.get("text", "")
                            if t:
                                parts.append(t)
                        elif ctype == "tool_use":
                            tname = c.get("name", "?")
                            tinput = c.get("input", {})
                            try:
                                tinput_s = json.dumps(tinput, ensure_ascii=False)
                            except Exception:
                                tinput_s = str(tinput)
                            parts.append(f"[tool_use:{tname}] {tinput_s}")
                        elif ctype == "tool_result":
                            tid = c.get("tool_use_id", "?")
                            body = _flatten_tool_result(c.get("content", ""))
                            if body.strip():
                                tag = "tool_error" if c.get("is_error") else "tool_result"
                                parts.append(f"[{tag}:{tid}]\n{body}")
                    content = "\n\n".join(parts)
                if role and isinstance(content, str) and content.strip():
                    out.append({"role": role, "content": content})
    except Exception:
        return []
    return out
