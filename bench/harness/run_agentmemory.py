"""agentmemory adapter for bench harness (skeleton).

agentmemory は Node.js/TypeScript 本体 + REST (port 3111) + MCP. Python から
は REST 経由で叩く. import-jsonl CLI もあるので transcript ロードは そちら
を使うのが速い可能性あり.

実装方針:
- 前提: `npx @agentmemory/agentmemory` をローカルで別プロセスで起動
- reset: REST で memory store を空に. もしくは server を再起動
- ingest_turn: POST /agentmemory/observe で 1 turn を流し込む
- query: POST /agentmemory/smart-search → 返ってきた memories を context
  にまとめ, 同じ Ollama LLM (gemma3:4b) に query と一緒に投げて回答生成
  (= persona-memory adapter と LLM を揃えて公平比較)

公平性メモ:
- 各 system は **default 設定のまま** 走らせる. agentmemory の compression LLM
  default は Anthropic. 持ち込みを揃えるのではなく「default 挙動でどの程度
  main agent token を削減できるか」 を測るのが本ベンチの目的.
- ANTHROPIC_API_KEY 必須. 月間制限に注意.
- 内部 LLM コスト (Anthropic 呼び出し回数 + token 数) も別途記録し,
  persona-memory のローカルゼロ円コストと並列表示する.
"""
from __future__ import annotations

# 実装は別 session で. 本ファイルは構造の placeholder.

raise NotImplementedError(
    "agentmemory adapter is a skeleton. Implement in a later session. "
    "Prereq: `npx @agentmemory/agentmemory` running on port 3111."
)
