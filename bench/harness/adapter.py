"""Bench MemoryAdapter インターフェース定義.

各 adapter (persona-memory / agentmemory / no_memory baseline) は本クラスを
継承して実装する. harness 側は adapter の種類を意識せず同一手順で評価する.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class QueryResult:
    answer: str
    injected_tokens: int            # 当該 probe 直前に main agent に渡された token 数
    elapsed_ms: int
    matched_keywords: list[str]     # evaluate.py 側で keyword hit 判定後に埋める


class MemoryAdapter(Protocol):
    """各 memory システムの薄いラッパ."""

    name: str  # "persona-memory" / "agentmemory" / "no_memory"

    def reset(self) -> None:
        """前回 run の状態を破棄. fresh DB / fresh server state.

        注意: 本物の DB を壊さないように、 adapter は **専用の tmp DB** を
        使う実装にする. test3 や dev DB は触らない.
        """
        ...

    def ingest_turn(self, turn: dict) -> None:
        """1 turn の発話を記憶側に流し込む.

        turn = {turn, role, content, ts, session_id}
        adapter は内部で write LLM 抽出や episode 保存等の自分の手順を踏む.
        """
        ...

    def query(self, probe: dict) -> QueryResult:
        """probe を投げて回答を取る.

        adapter は (a) 自分の recall を走らせて context を組み立て、
        (b) 同じ LLM (gemma3 等) に query を投げて回答を生成する.
        injected_tokens は (a) で main agent prompt に注入された分の概算.
        """
        ...
