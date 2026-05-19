"""Ollama-backed embedding + Lint judgment helpers."""
from __future__ import annotations

import json
import os
from typing import Sequence

import httpx

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
EMBED_MODEL = os.environ.get("PERSONA_EMBED_MODEL", "nomic-embed-text")
JUDGE_MODEL = os.environ.get("PERSONA_JUDGE_MODEL", "gemma3:4b")


class EmbeddingError(RuntimeError):
    pass


# nomic-embed-text のコンテキスト窓 (~2048 token) を超えると Ollama が
# 500 を返す. scripts/shared/embedding.py の同 constant に合わせる
# (server から scripts は import せず, 重複定義で疎結合を保つ).
EMBED_TEXT_MAX_CHARS = 3000


async def embed_text(text: str) -> list[float]:
    safe = "" if not text else text[:EMBED_TEXT_MAX_CHARS]
    if not safe:
        raise EmbeddingError("embed_text received empty text")
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            r = await client.post(
                f"{OLLAMA_HOST}/api/embeddings",
                json={"model": EMBED_MODEL, "prompt": safe},
            )
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise EmbeddingError(f"Ollama embedding failed: {e}") from e

    data = r.json()
    vec = data.get("embedding")
    if not isinstance(vec, list) or not vec:
        raise EmbeddingError(f"Ollama returned empty embedding: {data}")
    return vec


async def judge_conflict(fact_a: str, fact_b: str) -> tuple[bool, int]:
    prompt = (
        "You are a memory consistency checker. Given two facts, decide if they "
        "contradict each other (e.g. say opposite things about the same subject). "
        "If they are merely related or complementary, they do NOT contradict. "
        "Reply ONLY with strict JSON: {\"contradict\": true|false, \"confidence\": 0-100}.\n\n"
        f"Fact A: {fact_a}\nFact B: {fact_b}\n"
    )
    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            r = await client.post(
                f"{OLLAMA_HOST}/api/generate",
                json={
                    "model": JUDGE_MODEL,
                    "prompt": prompt,
                    "stream": False,
                    "format": "json",
                },
            )
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise EmbeddingError(f"Ollama judge failed: {e}") from e

    raw = r.json().get("response", "")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return (False, 0)
    return (bool(parsed.get("contradict", False)), int(parsed.get("confidence", 0)))


async def health() -> dict[str, str | bool]:
    async with httpx.AsyncClient(timeout=5.0) as client:
        try:
            r = await client.get(f"{OLLAMA_HOST}/api/tags")
            r.raise_for_status()
            tags = r.json().get("models", [])
            names = [m.get("name", "") for m in tags]
            return {
                "ollama_reachable": True,
                "embed_model_present": any(n.startswith(EMBED_MODEL) for n in names),
                "judge_model_present": any(n.startswith(JUDGE_MODEL) for n in names),
                "host": OLLAMA_HOST,
            }
        except httpx.HTTPError as e:
            return {
                "ollama_reachable": False,
                "host": OLLAMA_HOST,
                "error": str(e),
            }


def l2_normalize(vec: Sequence[float]) -> list[float]:
    """ベクトルを L2 正規化して list[float] で返す.

    旧 sqlite-vec 時代の `pack_embedding` の置き換え. SQLite-vec は廃止し
    Cozo 直叩きになったので bytes 詰め直しは不要 (Cozo は list[float] を
    受け取る). 正規化処理は既存 embedding データとの整合のため維持.
    """
    import math

    norm = math.sqrt(sum(x * x for x in vec))
    if norm > 0:
        return [x / norm for x in vec]
    return list(vec)
