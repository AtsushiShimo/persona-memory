# persona-memory ベンチマーク基盤

## 目的

persona-memory を agentmemory (主) と Letta 等 (副) に対して 3 つの KPI で比較:

1. **完全記憶** — 全発話が自動で記憶されているか
2. **思い出す** — 適切なタイミングで自動 recall できるか
3. **トークン** — 「過去全 turn を毎発話プロンプトに含める」 baseline と比べて
   90% 以上 削減できているか

短い会話では memory 機構の真価が問われないため、 **長尺の合成転写** に
**追跡 probe** を埋め込む形式で評価する.

## ディレクトリ

```
bench/
  scenarios/<id>-<name>/
    transcript.jsonl   # 1 行 = 1 turn ({role, content, ts, session_id})
    probes.yaml         # probe 群 (詳細は SCHEMA 参照)
    README.md           # シナリオの背景 / 想定 turn 数
  harness/
    SCHEMA.md           # transcript / probes / report の仕様
    adapter.py          # MemoryAdapter インターフェース定義
    run_persona_memory.py
    run_agentmemory.py
    run_no_memory.py    # baseline (= 過去全 turn を生 prompt にぶら下げる)
    evaluate.py         # keyword hit / token count の自動評価
  reports/<run-id>/
    summary.json
    per_probe.jsonl
```

## 走らせ方 (予定)

```bash
# 1 シナリオを全 adapter で走らせて比較
python -m bench.harness.run_persona_memory --scenario 01-renju-dev
python -m bench.harness.run_agentmemory    --scenario 01-renju-dev
python -m bench.harness.run_no_memory      --scenario 01-renju-dev

# 集計
python -m bench.harness.evaluate --scenario 01-renju-dev
```

## 公平性のルール

- **同じ転写・同じ probe** を全 adapter に投入する
- **各 system は default 設定のまま**走らせる. persona-memory は
  ローカル LLM (gemma3 + nomic-embed-text), agentmemory は default の
  Anthropic + all-MiniLM-L6-v2. **「どの LLM をどう使うか」 そのものが
  各 system の設計判断であり、 差し替えて公平にすると測定意味を失う**.
- 代わりに以下 2 軸を分けて測る:
  - **main agent context tokens**: probe 直前に「主たる対話 LLM」 へ
    渡された記憶/履歴の token 数 (= ユーザーが払う API 料金に直結)
  - **internal LLM cost**: 記憶側で消費した内部 LLM コスト
    (persona-memory = ローカル / agentmemory = Anthropic 等)
- **応答長制約** は全 adapter 共通 (= probe ごとに max 200 文字).
  recall ありなしで応答長が暴れて token KPI が歪まないように.
- **baseline (no_memory)** は会話履歴を「直近 N turn 全文」 として
  プロンプト末尾にぶら下げる. context window 限界を超える長さでは
  「先頭から切り落とす FIFO」 として評価.

### なぜ default を尊重するか

persona-memory はトークン削減のために **意図的にローカル LLM を採用** している.
agentmemory が Anthropic で 90% 削減を謳うなら, それが本当に達成できるかを
**default の挙動で**測るのが筋. 持ち込みを揃えて勝つ / 負けるは「実利用での
コスト構造」 を見失う.

## KPI の測定

| KPI | 測定方法 |
|---|---|
| 完全記憶 | transcript の N turn 目を probe で参照 → expected_keywords が回答に含まれるか |
| 思い出す | recall_required=True の probe で全 adapter の hit 率を比較 |
| トークン | 各 probe 直前に main agent へ注入された token 数 (system + injected memory + 直近 buffer) を計測. baseline と比較して削減率算出 |

## シナリオ一覧

- `01-renju-dev`: 五目並べ風ゲームの開発議論 (~50 turn, 30 probe). **本命**
- `02-pet-care`: 長期のペット飼育に関する継続会話 (skeleton)
- `03-knowledge-bank`: web 調査ナレッジ蓄積型 (skeleton)
