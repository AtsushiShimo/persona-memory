# Bench schema

## transcript.jsonl

1 行 1 turn. 厳密 JSON.

```json
{"turn": 1, "role": "user", "content": "Renju っぽいゲーム作りたい", "ts": "2026-04-01T10:00:00+09:00", "session_id": "s1"}
{"turn": 2, "role": "assistant", "content": "了解です。 まず盤サイズから決めましょうか", "ts": "2026-04-01T10:00:30+09:00", "session_id": "s1"}
```

- `turn`: 1-origin, 連番
- `role`: `user` | `assistant`
- `content`: 自然文 (改行可)
- `ts`: ISO8601 + offset
- `session_id`: 会話セッション id (= /clear で区切られる単位を模擬)

## probes.yaml

```yaml
- id: "01-renju-dev:p01"
  after_turn: 30                 # この turn まで会話を流し込んだ後で query を投げる
  query: "盤サイズって結局どうしたんだっけ?"
  expected_keywords:             # 1 つでも含めば正解
    - "15x15"
    - "15×15"
    - "15 x 15"
  recall_required: true          # 過去想起なしには答えられない
  distance: "mid"                # near (<10 turn) / mid (10-30) / far (30+)
  kind: "exact_fact"             # exact_fact / decision / retraction /
                                  # implicit_context / negation / numerical / name
  expected_answer: |
    最終的に 15x15 盤を採用しました.
    元々は 19x19 を検討していましたが計算量で諦めた経緯です.
  manual_review: false           # true なら自動評価不可, 人手レビュー前提
```

## report (summary.json)

```json
{
  "run_id": "2026-05-13T11-30_persona-memory_0.6.21",
  "scenario": "01-renju-dev",
  "adapter": "persona-memory",
  "total_probes": 30,
  "keyword_hit": 24,
  "keyword_hit_rate": 0.80,
  "avg_injected_tokens": 312,
  "p95_injected_tokens": 480,
  "baseline_avg_injected_tokens": 4200,
  "token_reduction_pct": 0.926
}
```

## per_probe.jsonl

1 行 1 probe.

```json
{"probe_id": "...", "answer": "...", "matched_keywords": ["15x15"], "injected_tokens": 312, "elapsed_ms": 8421}
```

## MemoryAdapter インターフェース

`bench/harness/adapter.py` で抽象クラス定義:

```python
class MemoryAdapter:
    def reset(self) -> None: ...
    def ingest_turn(self, turn: dict) -> None: ...
    def query(self, probe: dict) -> dict:  # → answer, injected_tokens, elapsed_ms
        ...
```

各 adapter (persona-memory / agentmemory / no-memory baseline) はこれを実装.
