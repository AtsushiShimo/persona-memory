"""recall hit を表すデータ構造 (db_cozo/recall_full から import).

0.8.1 で SQLite ベースの検索ロジック (search / _search_one / FTS / vec0
direct query / cluster dedup / scoring / bump_access_counts /
search_episodes_by_*) を削除し、 Cozo 直接実装の
`scripts.db_cozo.recall_full` に一本化. 残るのは Cozo 側からも返り値型
として使われる純粋な dataclass のみ.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RecalledFact:
    fact_id: int
    category: str
    key: str
    value: str
    importance: int
    access_count: int
    distance: float
    score: float
    # lint で auto_resolve された場合、撤回済の旧 value をここに添える.
    # 「以前は X と言っていたが撤回済み」 という補足を summarize LLM に渡し,
    # main エージェントの context に「両方提示+正解添え」 で流す.
    # source='lint_conflict' の supersede 時のみセットされる. None なら無し.
    retracted_value: str | None = None
    # 0.6.16 反事実記憶 Phase A2: 撤回理由 (旧 fact の reason_superseded).
    # conversation 由来の supersede でも write LLM が reason を抽出していれば
    # 添えられる. summarize 側で「(理由: Y) のため撤回済」 を出すための材料.
    retracted_reason: str | None = None


@dataclass
class RecalledEpisode:
    episode_id: int
    role: str
    content: str  # 切り詰め済み
    timestamp: str
