"""Ollama モデル pre-warm.

SessionStart hook から detach して呼ぶ. 空 prompt で /api/generate を叩いて
gemma3:12b と nomic-embed-text を load. keep_alive は OllamaClient 既定値
(30m) が効くため、その後 30 分は cold start なしで recall / write が応答する.

- ollama daemon 未起動 / モデル未 pull 等は静かに諦める (fail-open).
- 子プロセス前提 (sys.exit 後にハングしないよう httpx を直接叩く).
"""
from __future__ import annotations

import os
import sys

import httpx

HEAVY = os.environ.get("PERSONA_HEAVY_MODEL", "gemma3:12b")
LIGHT = os.environ.get("PERSONA_LIGHT_MODEL", "")  # 設定時のみ warm
EMBED = os.environ.get("PERSONA_EMBED_MODEL", "nomic-embed-text")
HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
KEEP_ALIVE = os.environ.get("PERSONA_OLLAMA_KEEP_ALIVE", "30m")
# Ollama は (model, num_ctx) ごとに別インスタンスを持つので、recall / write で
# 揃えた最大値 (write の 16384) で warm して以後の呼出を同一インスタンスに集約.
# 別 num_ctx で呼ばれると model swap → cold load (30-60s) が再発する.
PREWARM_NUM_CTX = int(os.environ.get("PERSONA_PREWARM_NUM_CTX", "16384"))


def _warm_generate(model: str) -> None:
    try:
        httpx.post(
            f"{HOST}/api/generate",
            json={"model": model, "prompt": "", "stream": False,
                  "keep_alive": KEEP_ALIVE,
                  "options": {"num_ctx": PREWARM_NUM_CTX}},
            timeout=300.0,
        )
    except Exception:
        pass


def _warm_embed(model: str) -> None:
    try:
        httpx.post(
            f"{HOST}/api/embeddings",
            json={"model": model, "prompt": "warmup", "keep_alive": KEEP_ALIVE},
            timeout=120.0,
        )
    except Exception:
        pass


def main() -> int:
    _warm_generate(HEAVY)
    if LIGHT and LIGHT != HEAVY:
        _warm_generate(LIGHT)
    _warm_embed(EMBED)
    return 0


if __name__ == "__main__":
    sys.exit(main())
