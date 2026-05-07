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
DEFAULT_TIMEOUT = float(os.environ.get("PERSONA_OLLAMA_TIMEOUT", "60.0"))


class LLMClient(Protocol):
    def generate(self, model: str, prompt: str) -> str: ...
    def embed(self, model: str, text: str) -> list[float]: ...


@dataclass
class OllamaClient:
    host: str = DEFAULT_HOST
    timeout: float = DEFAULT_TIMEOUT

    def generate(self, model: str, prompt: str) -> str:
        r = httpx.post(
            f"{self.host}/api/generate",
            json={"model": model, "prompt": prompt, "stream": False},
            timeout=self.timeout,
        )
        r.raise_for_status()
        return (r.json().get("response") or "").strip()

    def embed(self, model: str, text: str) -> list[float]:
        r = httpx.post(
            f"{self.host}/api/embeddings",
            json={"model": model, "prompt": text},
            timeout=self.timeout,
        )
        r.raise_for_status()
        return list(r.json().get("embedding") or [])
