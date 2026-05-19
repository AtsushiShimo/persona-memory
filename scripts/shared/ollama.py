"""Ollama HTTP client (mockable).

phase 3 では write LLM 経路で使う:
- generate(model, prompt) → 文字列レスポンス (fact 抽出)
- embed(model, text) → list[float] (類似判定)

mock 化はテストで OllamaClient のサブクラス or duck-typed fake を渡す。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol

import httpx

DEFAULT_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
# 60s では gemma3:12b の cold load (~30-60s) が間に合わず timeout する事例あり.
# warm 時は 1-3s で完結するので 180s でも実害は無い.
DEFAULT_TIMEOUT = float(os.environ.get("PERSONA_OLLAMA_TIMEOUT", "180.0"))
# keep_alive: アクセスから N の間モデル常駐. その後は Ollama 側で unload.
# 各リクエストの keep_alive 指定で Ollama 内のタイマーは再セットされる (= 連続
# 呼び出し中は再 load なし, アイドル時はメモリ解放).
# 0.8.6 改修: 30m → 3m に短縮 (RAM 占有を抑制. cold start は次回呼出時に許容).
# 「-1」 で永続常駐、 環境変数 PERSONA_OLLAMA_KEEP_ALIVE で上書き可.
DEFAULT_KEEP_ALIVE = os.environ.get("PERSONA_OLLAMA_KEEP_ALIVE", "3m")


class LLMClient(Protocol):
    def generate(self, model: str, prompt: str) -> str: ...
    def embed(self, model: str, text: str) -> list[float]: ...


@dataclass
class OllamaClient:
    host: str = DEFAULT_HOST
    timeout: float = DEFAULT_TIMEOUT

    def generate(self, model: str, prompt: str, num_ctx: int | None = None) -> str:
        # num_ctx: per-request context window (token). 指定すると KV cache 配分が
        # 縮み、モデル併存時の memory pressure を緩和できる. 既定 None = モデル既定値.
        payload: dict = {
            "model": model, "prompt": prompt, "stream": False,
            "keep_alive": DEFAULT_KEEP_ALIVE,
        }
        if num_ctx is not None:
            payload["options"] = {"num_ctx": num_ctx}
        r = httpx.post(
            f"{self.host}/api/generate",
            json=payload,
            timeout=self.timeout,
        )
        r.raise_for_status()
        return (r.json().get("response") or "").strip()

    def embed(self, model: str, text: str) -> list[float]:
        # nomic-embed-text 等のコンテキスト窓超過 (~2048 token) で 500 が返る
        # 事故を防ぐため、 入力テキストを安全長に切り詰めてから投げる.
        # raw 側は呼出元で無傷に保存される.
        from scripts.shared.embedding import truncate_for_embedding
        safe = truncate_for_embedding(text)
        if not safe:
            return []
        r = httpx.post(
            f"{self.host}/api/embeddings",
            json={"model": model, "prompt": safe, "keep_alive": DEFAULT_KEEP_ALIVE},
            timeout=self.timeout,
        )
        r.raise_for_status()
        return list(r.json().get("embedding") or [])
