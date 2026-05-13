"""Baseline: 過去全 turn を毎発話プロンプトにぶら下げる (= no memory) adapter.

トークン KPI の分母. memory 機構を使わず, 直近 N turn をそのまま末尾に
連結して LLM に渡す. context window 限界を超えたら **古い turn から FIFO**.

実装方針:
- reset: 内部 buffer をクリア
- ingest_turn: buffer に append するだけ
- query: probe.query を投げる prompt を組み立てる:
    [buffer の全 turn 連結] + "\n\nUser: {query}"
  これを同じ Ollama LLM (gemma3:4b) に投げる. injected_tokens =
  prompt 全文の char/2.5 近似.

注意: 公平比較のため persona-memory / agentmemory と同じ LLM を使う.
モデル違いで応答品質が暴れる現象を排除.
"""
from __future__ import annotations

# 実装は別 session で. 本ファイルは構造の placeholder.

raise NotImplementedError(
    "no_memory baseline adapter is a skeleton. Implement in a later session."
)
