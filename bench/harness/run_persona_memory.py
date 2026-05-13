"""persona-memory adapter for bench harness (skeleton).

実装方針:
- 専用 tmp DB (例: bench/.tmp/<run-id>.db) を init して **本番 DB は触らない**
- 1 turn 流し込み = save_episode → 同期で write/run.process_episode を呼ぶ
  (本番 hook と同じ経路を再現. write LLM 呼び出し含む)
- query: scripts/recall/run.recall() を直接呼ぶ. 返ってきた additional_context を
  token count + 同じ Ollama LLM (gemma3:4b) に query と一緒に投げて回答生成

注意:
- write/recall とも LLM を呼ぶので **time 重い**. 1 turn 数秒 + 各 probe 数秒.
  シナリオ 1 (50 turn + 30 probe) で 5-10 分相当.
- injected_tokens は additionalContext の文字数 / 2.5 で近似. 後で tiktoken
  等で厳密化する余地あり (= 後日 task).
"""
from __future__ import annotations

# 実装は別 session で. 本ファイルは構造の placeholder.

raise NotImplementedError(
    "persona-memory adapter is a skeleton. Implement in a later session. "
    "See bench/harness/SCHEMA.md for the MemoryAdapter contract."
)
