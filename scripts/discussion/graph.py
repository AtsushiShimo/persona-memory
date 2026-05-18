"""議論グラフの kind / state / edge_kind 定数 (write/extract.py から import).

0.8.1 で SQLite 直接 CRUD 関数群 (add_node / add_edge / transition_state /
supersede_node / latest_decision_for_topic / neighbors /
nearest_discussion_nodes) を削除し、 Cozo 直接実装の
`scripts.db_cozo.discussion` に一本化. 残るのは write LLM が NodeCandidate を
バリデートする際に共有する語彙定数のみ.

ノード kind:
  topic       — 論点 / 問い
  option      — 検討案 / 選択肢
  decision    — 採用判断 (state='accepted' で「決まった」 とみなす)
  retraction  — 明示的な撤回
  rationale   — 理由・前提

ノード state:
  proposed    — 提案中
  accepted    — 採用済 (= 結論)
  rejected    — 却下
  superseded  — 後続で上書きされた (履歴として残す)
  observed    — 観察事実 (topic 以外で proposed/accepted/rejected の枠に乗らないもの)

エッジ kind:
  considers     — topic → option (この論点はこの案を含む)
  decides       — option → decision (この案を採用)
  retracts      — retraction → 任意 (撤回)
  supersedes    — 新 → 旧 (上書き)
  derives_from  — 派生 (任意 → 任意)
  depends_on    — 前提 (任意 → 任意)
"""
from __future__ import annotations

VALID_KINDS = {"topic", "option", "decision", "retraction", "rationale"}
VALID_STATES = {"proposed", "accepted", "rejected", "superseded", "observed"}
VALID_EDGE_KINDS = {
    "considers", "decides", "retracts", "supersedes", "derives_from", "depends_on",
}
