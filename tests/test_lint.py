"""scripts.lint.run のテスト (Unit 2 範囲).

LLM (judge_conflict) は mock し、judge の結果に応じて facts / conflicts /
lint_log / fact_embeddings がどう変わるかを検証する.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from scripts.db.connection import connect
from scripts.db.migrate import init_db
from scripts.lint.run import (
    AUTO_RESOLVE_THRESHOLD, FLAG_THRESHOLD,
    judge_conflict, lint_around_fact, run as lint_run,
)
from scripts.write.extract import FactCandidate
from scripts.write.persist import insert_new


@dataclass
class FakeJudgeClient:
    """judge_conflict 用 LLM mock.

    `judgments` は (value_a, value_b) → (contradict, confidence) の dict.
    embed は受け取った text → そのままハッシュ的な vector を返し、
    near neighbor 関係を簡易に作れる.
    """
    judgments: dict[tuple[str, str], tuple[bool, int]] = field(default_factory=dict)
    embedding_map: dict[str, list[float]] = field(default_factory=dict)
    default_embedding: list[float] = field(default_factory=lambda: [1.0] + [0.0] * 767)

    def generate(self, model: str, prompt: str, num_ctx: int | None = None) -> str:
        # judge prompt はテンプレートに「記憶 A: ... 記憶 B: ...」 を含む
        # value 抽出
        import re
        m = re.search(r"記憶 A: (.+?)\n記憶 B: (.+?)\n", prompt)
        if not m:
            return json.dumps({"contradict": False, "confidence": 0})
        a, b = m.group(1).strip(), m.group(2).strip()
        # どちらの順序でも引けるように両方試す
        for key in ((a, b), (b, a)):
            if key in self.judgments:
                contradict, conf = self.judgments[key]
                return json.dumps({"contradict": contradict, "confidence": conf})
        return json.dumps({"contradict": False, "confidence": 0})

    def embed(self, model: str, text: str) -> list[float]:
        return list(self.embedding_map.get(text, self.default_embedding))


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    p = tmp_path / "p.db"
    init_db(p)
    return p


def _seed_two_facts(conn, value_a: str, value_b: str,
                    cat: str = "preference",
                    key_a: str = "opt_taste", key_b: str = "alt_taste",
                    same_vector: bool = True) -> tuple[int, int]:
    """2 つの fact を入れる. same_vector=True なら同じ vec で近傍判定が必ず通る.

    same_vector=False は直交 vector で cosine distance=1.0 になるよう作る.

    default key は **末尾単語が同じ** (= 'taste') にして 0.5.17 の同属性
    判定 (`_is_same_attribute`) を通るようにする. 異属性のテストは明示的に
    末尾の違う key (例: 'pet_dog_name' / 'pet_dog_breed') を渡す.
    """
    vec_a = [1.0] + [0.0] * 767
    vec_b = vec_a if same_vector else [0.0, 1.0] + [0.0] * 766  # 直交
    aid = insert_new(conn, FactCandidate(cat, key_a, value_a, 7), vec_a)
    bid = insert_new(conn, FactCandidate(cat, key_b, value_b, 7), vec_b)
    conn.commit()
    return aid, bid


# ── judge_conflict 単体 ────────────────────────────────────────────────────

def test_judge_conflict_parses_valid_json():
    client = FakeJudgeClient(judgments={("X", "Y"): (True, 95)})
    contradict, conf = judge_conflict("X", "Y", client)
    assert contradict is True
    assert conf == 95


def test_judge_conflict_returns_zero_on_garbage_response():
    class GarbageClient:
        def generate(self, m, p, num_ctx=None): return "なんか壊れたレスポンス"
        def embed(self, m, t): return [0.0] * 768
    contradict, conf = judge_conflict("a", "b", GarbageClient())
    assert (contradict, conf) == (False, 0)


def test_judge_conflict_clamps_confidence_in_range():
    """LLM が confidence=200 を返しても 0-100 に clamp される."""
    class WildClient:
        def generate(self, m, p, num_ctx=None):
            return json.dumps({"contradict": True, "confidence": 200})
        def embed(self, m, t): return [0.0] * 768
    _, conf = judge_conflict("a", "b", WildClient())
    assert conf == 100


# ── lint_around_fact: 自動解消 (>=90) ────────────────────────────────────

def test_auto_supersedes_old_fact_when_high_confidence(db_path):
    conn = connect(db_path)
    try:
        a, b = _seed_two_facts(conn, "深煎り派", "浅煎りが好き")
        client = FakeJudgeClient(
            judgments={("深煎り派", "浅煎りが好き"): (True, 95)},
        )
        result = lint_around_fact(conn, b, client)  # 新しい方を起点
        assert result["auto_resolved"] == 1
        assert result["flagged"] == 0
        # 古い (a) が superseded、新 (b) が active
        rows = {r[0]: r for r in conn.execute(
            "SELECT id, status, source, supersedes, superseded_by FROM facts ORDER BY id"
        ).fetchall()}
        assert rows[a][1] == "superseded"
        assert rows[a][2] == "lint_conflict"
        assert rows[a][4] == b  # superseded_by = b
        assert rows[b][1] == "active"
        assert rows[b][3] == a  # supersedes = a
        # fact_embeddings から旧 a が削除
        rem = conn.execute(
            "SELECT COUNT(*) FROM fact_embeddings WHERE fact_id=?", (a,),
        ).fetchone()[0]
        assert rem == 0
        # conflicts 行が記録されている
        c = conn.execute(
            "SELECT fact_a_id, fact_b_id, confidence, resolution FROM conflicts"
        ).fetchone()
        assert c == (b, a, 95, "auto_superseded")
    finally:
        conn.close()


# ── lint_around_fact: flag (60-89) ──────────────────────────────────────

def test_flagged_when_confidence_in_middle_range(db_path):
    conn = connect(db_path)
    try:
        a, b = _seed_two_facts(conn, "X 派", "Y 派")
        client = FakeJudgeClient(
            judgments={("X 派", "Y 派"): (True, 75)},
        )
        result = lint_around_fact(conn, b, client)
        assert result["flagged"] == 1
        assert result["auto_resolved"] == 0
        # 両方 active のまま (recall に出る)
        statuses = [r[0] for r in conn.execute(
            "SELECT status FROM facts ORDER BY id"
        ).fetchall()]
        assert statuses == ["active", "active"]
        # conflicts に flagged で記録
        c = conn.execute(
            "SELECT confidence, resolution FROM conflicts"
        ).fetchone()
        assert c == (75, "flagged")
    finally:
        conn.close()


# ── lint_around_fact: 矛盾なし / 低 confidence ─────────────────────────

def test_no_action_when_not_contradicting(db_path):
    conn = connect(db_path)
    try:
        _seed_two_facts(conn, "深煎り派", "砂糖は入れない")
        # judgments 未登録 = (False, 0) → 何もしない
        client = FakeJudgeClient()
        result = lint_around_fact(conn, 2, client)
        assert result == {"pairs_examined": 1, "flagged": 0, "auto_resolved": 0}
        cnt = conn.execute("SELECT COUNT(*) FROM conflicts").fetchone()[0]
        assert cnt == 0
    finally:
        conn.close()


def test_no_action_when_confidence_below_flag_threshold(db_path):
    conn = connect(db_path)
    try:
        _seed_two_facts(conn, "X", "Y")
        client = FakeJudgeClient(
            judgments={("X", "Y"): (True, 55)},  # below FLAG_THRESHOLD=60
        )
        result = lint_around_fact(conn, 2, client)
        assert result == {"pairs_examined": 1, "flagged": 0, "auto_resolved": 0}
    finally:
        conn.close()


# ── 近傍が distance_max を超えていれば対象外 ───────────────────────────

def test_skips_far_neighbors(db_path, monkeypatch):
    """fact 起点の query embedding が他 fact と直交していれば対象外."""
    monkeypatch.setenv("PERSONA_LINT_DISTANCE_MAX", "0.5")
    import importlib, scripts.lint.run as L
    importlib.reload(L)

    conn = connect(db_path)
    try:
        _seed_two_facts(conn, "A", "B", same_vector=False)  # 直交 vector で seed
        # 起点 fact (id=2, key=alt_taste) の embed query が a (key=opt_taste) と直交
        client = FakeJudgeClient(
            judgments={("A", "B"): (True, 95)},
            embedding_map={"preference/alt_taste: B": [0.0, 1.0] + [0.0] * 766},
        )
        result = L.lint_around_fact(conn, 2, client)
        assert result["pairs_examined"] == 0
    finally:
        conn.close()
        monkeypatch.delenv("PERSONA_LINT_DISTANCE_MAX")
        importlib.reload(L)


# ── seen_pairs で同 pair を 2 度判定しない ────────────────────────────

def test_seen_pairs_dedup(db_path):
    conn = connect(db_path)
    try:
        a, b = _seed_two_facts(conn, "X", "Y")
        client = FakeJudgeClient(judgments={("X", "Y"): (True, 95)})
        seen: set[tuple[int, int]] = set()
        r1 = lint_around_fact(conn, a, client, seen)
        r2 = lint_around_fact(conn, b, client, seen)
        # 1 回目で auto_resolve, 2 回目は seen で skip
        assert r1["auto_resolved"] == 1
        assert r2["pairs_examined"] == 0
    finally:
        conn.close()


# ── run() entry: lint_log に集計 1 行 ─────────────────────────────────

def test_run_records_lint_log(db_path, monkeypatch):
    monkeypatch.setenv("PERSONA_MEMORY_DB", str(db_path))
    conn = connect(db_path)
    try:
        a, b = _seed_two_facts(conn, "X", "Y")
    finally:
        conn.close()

    client = FakeJudgeClient(judgments={("X", "Y"): (True, 95)})
    totals = lint_run([a, b], client=client)
    assert totals["auto_resolved"] == 1

    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT pairs_examined, conflicts_flagged, conflicts_auto_resolved, trigger_kind "
            "FROM lint_log"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    assert rows[0] == (1, 0, 1, "write_tail")


# ── 定数 sanity ────────────────────────────────────────────────────────

def test_thresholds_have_expected_default_values():
    assert AUTO_RESOLVE_THRESHOLD == 90
    assert FLAG_THRESHOLD == 60


def test_lint_skips_same_category_different_attribute(db_path):
    """同 category だが key 末尾の単語が違えば judge 対象外 (0.5.17 で追加).

    例: profile/pet_dog_name=まろん と profile/pet_dog_breed=ミニチュア…は
    別属性 (name vs breed) なので judge LLM の暴走を防ぐ.
    """
    conn = connect(db_path)
    try:
        vec = [1.0] + [0.0] * 767
        a = insert_new(conn, FactCandidate("profile", "pet_dog_name", "まろん", 7), vec)
        b = insert_new(conn, FactCandidate("profile", "pet_dog_breed",
                                            "ミニチュアダックスフンド", 7), vec)
        conn.commit()
        client = FakeJudgeClient(
            judgments={
                ("まろん", "ミニチュアダックスフンド"): (True, 95),
                ("ミニチュアダックスフンド", "まろん"): (True, 95),
            },
        )
        result = lint_around_fact(conn, a, client)
        # 同 cat 同属性 fact が他にいないので pairs_examined=0
        assert result["pairs_examined"] == 0
        # 両方 active
        statuses = [r[0] for r in conn.execute(
            "SELECT status FROM facts WHERE id IN (?,?) ORDER BY id", (a, b),
        ).fetchall()]
        assert statuses == ["active", "active"]
    finally:
        conn.close()


def test_lint_skips_facts_in_different_category(db_path):
    """異 category fact (例: profile vs preference) は近傍として
    取り出されないので judge 対象外 (= 0.5.16 で追加した暴走防止)."""
    conn = connect(db_path)
    try:
        # 同 vec で 2 fact を seed するが category が違う
        vec = [1.0] + [0.0] * 767
        a = insert_new(conn, FactCandidate("preference", "coffee", "深煎り", 7), vec)
        b = insert_new(conn, FactCandidate("profile", "pet_name", "まろん", 7), vec)
        conn.commit()
        # judge は呼ばれてはいけない (= contradict と判定されても無視される
        # 設計だが、そもそも _fetch_neighbors で除外されている)
        client = FakeJudgeClient(
            judgments={
                ("深煎り", "まろん"): (True, 95),  # 仮に矛盾と判定しても
                ("まろん", "深煎り"): (True, 95),
            },
        )
        result = lint_around_fact(conn, a, client)
        # 同 cat に他 fact が無いので pairs_examined=0
        assert result["pairs_examined"] == 0
        assert result["auto_resolved"] == 0
        # 両方 active のまま
        statuses = [r[0] for r in conn.execute(
            "SELECT status FROM facts WHERE id IN (?,?) ORDER BY id", (a, b),
        ).fetchall()]
        assert statuses == ["active", "active"]
    finally:
        conn.close()


def test_recall_search_attaches_retracted_value_for_lint_conflict(db_path):
    """lint_conflict で supersede された旧 value が active fact の retracted_value
    に乗ること (= recall で「両方提示+正解添え」 を main に流す経路)."""
    from scripts.recall.search import search

    conn = connect(db_path)
    try:
        # 同じ vec で 2 fact を seed: 古い側 (a) が後で superseded になる
        a, b = _seed_two_facts(conn, "深煎り派", "浅煎りが好き")
        client = FakeJudgeClient(
            judgments={("深煎り派", "浅煎りが好き"): (True, 95)},
        )
        # b 側起点で lint → a が superseded + source='lint_conflict'
        result = lint_around_fact(conn, b, client)
        assert result["auto_resolved"] == 1

        # recall search: query embedding は default ([1,0,...])。fact b と一致.
        hits = search(conn, [[1.0] + [0.0] * 767])
        assert len(hits) == 1
        h = hits[0]
        assert h.fact_id == b
        assert h.value == "浅煎りが好き"
        assert h.retracted_value == "深煎り派"  # ← lint で撤回された旧 value
    finally:
        conn.close()


def test_recall_search_no_retracted_for_normal_supersede(db_path):
    """通常の write supersede (source='conversation') では retracted_value=None."""
    from scripts.recall.search import search
    from scripts.write.persist import supersede
    from scripts.write.similarity import Match

    conn = connect(db_path)
    try:
        a, b = _seed_two_facts(conn, "古い", "新しい")
        # 通常の supersede (lint ではなく write 経路)
        supersede(
            conn,
            Match(fact_id=a, category="preference", key="a",
                  value="古い", importance=7, method="key"),
            FactCandidate("preference", "a", "新しい更新", 7),
            embedding=[1.0] + [0.0] * 767,
            source="conversation",
        )
        conn.commit()
        hits = search(conn, [[1.0] + [0.0] * 767])
        # 通常 supersede 由来の active fact には retracted_value 付かない
        active_hits = [h for h in hits if h.retracted_value is not None]
        assert active_hits == []
    finally:
        conn.close()
