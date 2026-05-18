"""seed_persona の統合テスト — Ollama 落ちでも persona facts は seed される."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.db.connection import connect
from scripts.db.migrate import init_db
from scripts.seed_persona import _normalize_address_user, seed


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    p = tmp_path / "p.db"
    init_db(p)
    return p


def _seed_kwargs() -> dict:
    return dict(
        role="バックエンドエンジニアの相棒",
        name="テスト太郎",
        gender="男性",
        personality="冷静沈着",
        first_person="僕",
        speech_style="敬語 (丁寧)",
        address_user="あなた",
    )


def test_seed_inserts_all_boot_facts(db_path: Path):
    """boot 層 = persona 17 + rule 2 = 19 件 seed される
    (0.7.8 で playbook_debug_mode_request 追加)."""
    with patch("scripts.seed_persona.OllamaClient") as Mock:
        Mock.return_value.embed.return_value = []
        seed(db_path, **_seed_kwargs())

    conn = connect(db_path)
    try:
        persona_rows = conn.execute(
            "SELECT key FROM facts WHERE category='persona' ORDER BY id"
        ).fetchall()
        rule_rows = conn.execute(
            "SELECT key FROM facts WHERE category='rule' ORDER BY id"
        ).fetchall()
    finally:
        conn.close()

    persona_keys = [r[0] for r in persona_rows]
    rule_keys = [r[0] for r in rule_rows]

    # persona: 7 基本 + 10 default 行動指針 = 17
    assert len(persona_rows) == 17
    for k in (
        "role", "identity", "personality", "gender", "first_person",
        "speech_style", "address_user",
        "response_brevity", "confirmation_before_acting", "silent_memory",
        "natural_voice", "health_check_trigger", "explicit_recall_via_mcp",
        "no_hallucinated_user_facts",
        "playbook_after_web_research",
        "playbook_continue_topic",
        "playbook_debug_mode_request",
    ):
        assert k in persona_keys, f"missing persona/{k}"

    # rule: no_direct_memory_lookup + forbid_auto_memory
    assert len(rule_rows) == 2
    assert "no_direct_memory_lookup" in rule_keys
    assert "forbid_auto_memory" in rule_keys


def test_seed_writes_embeddings_when_available(db_path: Path):
    fake_vec = [0.1] * 768
    with patch("scripts.seed_persona.OllamaClient") as Mock:
        Mock.return_value.embed.return_value = fake_vec
        seed(db_path, **_seed_kwargs())

    conn = connect(db_path)
    try:
        n = conn.execute("SELECT COUNT(*) FROM fact_embeddings").fetchone()[0]
    finally:
        conn.close()
    assert n == 19  # persona 17 + rule 2 (0.7.8 で +1)


def test_seed_idempotent(db_path: Path):
    """同じ key で 2 回 seed しても重複しない (active 1 件のみ)。"""
    with patch("scripts.seed_persona.OllamaClient") as Mock:
        Mock.return_value.embed.return_value = []
        seed(db_path, **_seed_kwargs())
        seed(db_path, **{**_seed_kwargs(), "personality": "明るく前向き"})  # 値変更

    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT value FROM facts WHERE category='persona' AND key='personality'"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    assert rows[0][0] == "明るく前向き"


def test_seed_persona_facts_are_boot_layer(db_path: Path):
    """seed されたものは on_session_start が boot 層として注入できる。"""
    from scripts.boot.inject import fetch_boot_facts

    with patch("scripts.seed_persona.OllamaClient") as Mock:
        Mock.return_value.embed.return_value = []
        seed(db_path, **_seed_kwargs())

    conn = connect(db_path)
    try:
        facts = fetch_boot_facts(conn)
    finally:
        conn.close()

    # seed されるのは persona 17 + rule 2 = 19 件だが、
    # fetch_boot_facts は playbook_* (= 状況依存ノウハウ) を除外するため
    # SessionStart 注入対象は 16 件
    # (playbook_after_web_research / playbook_continue_topic / playbook_debug_mode_request が除外)
    assert len(facts) == 16
    cats = {f["category"] for f in facts}
    assert cats == {"persona", "rule"}


def test_seed_without_stance_does_not_create_stance_fact(db_path: Path):
    """stance 省略時は persona/stance fact が作られない (後方互換)."""
    with patch("scripts.seed_persona.OllamaClient") as Mock:
        Mock.return_value.embed.return_value = []
        seed(db_path, **_seed_kwargs())  # stance 引数なし

    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT id FROM facts WHERE category='persona' AND key='stance'"
        ).fetchone()
    finally:
        conn.close()
    assert row is None


def test_seed_with_stance_creates_natural_language_fact(db_path: Path):
    """stance 5 値で seed → persona/stance fact が自然語 value で作られる."""
    with patch("scripts.seed_persona.OllamaClient") as Mock:
        Mock.return_value.embed.return_value = []
        # 革新 +1 / 悲観 0 / 分析 -1 / 大胆 +2 / 共感 -1
        seed(db_path, **{**_seed_kwargs(), "stance": [1, 0, -1, 2, -1]})

    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT value, importance FROM facts "
            "WHERE category='persona' AND key='stance' AND status='active'"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    value, importance = row[0], row[1]
    assert importance == 9
    # 5 軸全てが value に含まれている
    assert "保守-革新軸" in value
    assert "楽観-悲観軸" in value
    assert "直感-分析軸" in value
    assert "慎重-大胆軸" in value
    assert "共感-論理軸" in value
    # 各タグの方向が値どおりに記述されている
    assert "やや革新寄り" in value     # +1
    assert "バランス" in value          # 0
    assert "やや直感寄り" in value     # -1 (左 = 直感)
    assert "強く大胆寄り" in value     # +2
    assert "やや共感寄り" in value     # -1


def test_add_stance_to_db_without_existing_stance(db_path: Path):
    """既存 stance fact が無い DB に add_stance で追加できる."""
    from scripts.add_stance import add_stance_if_missing
    # まず stance なしで seed
    with patch("scripts.seed_persona.OllamaClient") as Mock:
        Mock.return_value.embed.return_value = []
        seed(db_path, **_seed_kwargs())  # stance なし
    # add_stance を呼ぶ
    with patch("scripts.add_stance.OllamaClient") as Mock2:
        Mock2.return_value.embed.return_value = []
        result = add_stance_if_missing(db_path, [1, 0, -1, 2, -1])
    assert result == "added"
    # DB に persona/stance が active で入っている
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT value, importance FROM facts "
            "WHERE category='persona' AND key='stance' AND status='active'"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row[1] == 9
    assert "やや革新寄り" in row[0]
    assert "強く大胆寄り" in row[0]


def test_add_stance_skips_if_already_set(db_path: Path):
    """既に persona/stance が設定済みなら何もしない."""
    from scripts.add_stance import add_stance_if_missing
    # stance 付きで seed
    with patch("scripts.seed_persona.OllamaClient") as Mock:
        Mock.return_value.embed.return_value = []
        seed(db_path, **{**_seed_kwargs(), "stance": [0, 0, 0, 0, 0]})
    # 別の値で add_stance を呼ぶ
    with patch("scripts.add_stance.OllamaClient") as Mock2:
        Mock2.return_value.embed.return_value = []
        result = add_stance_if_missing(db_path, [2, 2, 2, 2, 2])
    assert result == "exists"
    # value は元の (0,0,0,0,0 = 全部バランス) のまま
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT value FROM facts "
            "WHERE category='persona' AND key='stance' AND status='active'"
        ).fetchone()
    finally:
        conn.close()
    assert "バランス" in row[0]
    assert "強く" not in row[0]  # 上書きされていない


def test_add_stance_marks_boot_dirty(db_path: Path):
    """add_stance 成功時に boot 層 dirty フラグが立つ (= 次発話で再注入)."""
    from scripts.add_stance import add_stance_if_missing
    from scripts.boot.inject import is_dirty

    with patch("scripts.seed_persona.OllamaClient") as Mock:
        Mock.return_value.embed.return_value = []
        seed(db_path, **_seed_kwargs())

    conn = connect(db_path)
    assert is_dirty(conn) is False  # seed 直後は dirty なし
    conn.close()

    with patch("scripts.add_stance.OllamaClient") as Mock2:
        Mock2.return_value.embed.return_value = []
        add_stance_if_missing(db_path, [1, 0, 0, 0, 0])

    conn = connect(db_path)
    try:
        assert is_dirty(conn) is True
    finally:
        conn.close()


def test_parse_stance_csv_validates_format():
    """--stance CSV のパーサが要素数 / 範囲 / 整数を検証."""
    from scripts.seed_persona import _parse_stance_csv

    # 正常
    assert _parse_stance_csv("0,1,-1,2,-2") == [0, 1, -1, 2, -2]
    assert _parse_stance_csv(" 0 , 1 , -1 , 2 , -2 ") == [0, 1, -1, 2, -2]

    # 異常
    with pytest.raises(ValueError, match="5 要素必要"):
        _parse_stance_csv("0,1,-1")
    with pytest.raises(ValueError, match="整数でない"):
        _parse_stance_csv("0,1,abc,2,-1")
    with pytest.raises(ValueError, match=r"-2\.\.\+2"):
        _parse_stance_csv("0,1,-3,2,-1")
    with pytest.raises(ValueError, match=r"-2\.\.\+2"):
        _parse_stance_csv("0,1,1,2,3")


# ── address_user 正規化 (LLM の name slot 捏造誘発を防ぐ) ─────────────────

def test_normalize_address_user_strips_placeholder_tilde():
    """`〜さん (敬称)` のような placeholder は『あなた』 系へ展開される."""
    out = _normalize_address_user("〜さん (敬称)")
    assert "〜" not in out
    assert "あなた" in out
    # 全角チルダ ～ も同様
    out_full = _normalize_address_user("～さん")
    assert "〜" not in out_full and "～" not in out_full
    assert "あなた" in out_full


def test_normalize_address_user_strips_suffix_label():
    """末尾の ` (敬称)` 補注は剥がす (placeholder 化を防ぐ)."""
    assert _normalize_address_user("マスター (敬称)") == "マスター"


def test_normalize_address_user_passes_concrete_values():
    """具体的な呼称はそのまま (副作用を起こさない)."""
    for s in ("マスター", "あなた", "君", "ユーザーさん"):
        assert _normalize_address_user(s) == s


def test_seed_stores_normalized_address_user(db_path: Path):
    """placeholder で seed → DB には正規化された value が入る."""
    with patch("scripts.seed_persona.OllamaClient") as Mock:
        Mock.return_value.embed.return_value = []
        seed(db_path, **{**_seed_kwargs(), "address_user": "〜さん (敬称)"})

    conn = connect(db_path)
    try:
        v = conn.execute(
            "SELECT value FROM facts WHERE category='persona' AND key='address_user' AND status='active'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert "〜" not in v
    assert "あなた" in v
