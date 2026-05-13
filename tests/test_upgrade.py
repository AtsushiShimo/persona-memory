"""upgrade (non-destructive default refresh) のテスト."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.boot.defaults import DEFAULT_BOOT_FACTS
from scripts.db.connection import connect
from scripts.db.migrate import init_db
from scripts.seed_persona import seed
from scripts.upgrade import upgrade


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


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    p = tmp_path / "p.db"
    init_db(p)
    return p


def test_upgrade_unchanged_after_fresh_seed(db_path: Path):
    """seed 直後に upgrade を走らせると、何も変わらない (idempotent)."""
    with patch("scripts.seed_persona.OllamaClient") as Mock:
        Mock.return_value.embed.return_value = []
        seed(db_path, **_seed_kwargs())

    with patch("scripts.upgrade.OllamaClient") as Mock:
        Mock.return_value.embed.return_value = []
        counts = upgrade(db_path)

    assert counts["inserted"] == 0
    assert counts["updated"] == 0
    assert counts["unchanged"] == len(DEFAULT_BOOT_FACTS)
    assert counts["deprecated"] == 0


def test_upgrade_deprecates_old_default(db_path: Path):
    """DEPRECATED_BOOT_FACTS にある active 行を superseded に降格."""
    with patch("scripts.seed_persona.OllamaClient") as Mock:
        Mock.return_value.embed.return_value = []
        seed(db_path, **_seed_kwargs())

    # 過去のバージョンが焼いた廃止 fact を仕込む
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO facts(category, key, value, importance) "
            "VALUES ('rule', 'session_title_prefix', 'OLD_VALUE', 9)"
        )
        conn.commit()
    finally:
        conn.close()

    with patch("scripts.upgrade.OllamaClient") as Mock:
        Mock.return_value.embed.return_value = []
        counts = upgrade(db_path)

    assert counts["deprecated"] >= 1

    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT status FROM facts "
            "WHERE category='rule' AND key='session_title_prefix'"
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == "superseded"


def test_upgrade_deprecate_no_op_when_absent(db_path: Path):
    """廃止 fact が DB に無ければ deprecated カウントは 0."""
    with patch("scripts.seed_persona.OllamaClient") as Mock:
        Mock.return_value.embed.return_value = []
        seed(db_path, **_seed_kwargs())

    with patch("scripts.upgrade.OllamaClient") as Mock:
        Mock.return_value.embed.return_value = []
        counts = upgrade(db_path)

    assert counts["deprecated"] == 0


def test_upgrade_backfills_missing_episode_embeddings(db_path: Path):
    """既存 episodes で episode_embeddings 未登録のものを backfill する."""
    from scripts.db.connection import connect
    from scripts.db.repo import save_episode

    # episodes を 3 件追加 (どれも episode_embeddings には未登録)
    conn = connect(db_path)
    try:
        save_episode(conn, "user", "コーヒー好き", "s1")
        save_episode(conn, "assistant", "了解", "s1")
        save_episode(conn, "user", "ペットの話", "s1")
    finally:
        conn.close()

    # backfill 走らせる
    fake_vec = [0.1] * 768
    with patch("scripts.upgrade.OllamaClient") as Mock:
        Mock.return_value.embed.return_value = fake_vec
        counts = upgrade(db_path)

    assert counts["episodes_embedded"] == 3

    # episode_embeddings に 3 件入った
    conn = connect(db_path)
    try:
        n = conn.execute("SELECT COUNT(*) FROM episode_embeddings").fetchone()[0]
    finally:
        conn.close()
    assert n == 3


def test_upgrade_skips_already_embedded_episodes(db_path: Path):
    """既に episode_embeddings に登録済みの episode は backfill しない."""
    from scripts.db.connection import connect
    from scripts.db.repo import save_episode
    from scripts.shared.embedding import pack

    conn = connect(db_path)
    try:
        eid1 = save_episode(conn, "user", "既存の埋め込み済み", "s1")
        save_episode(conn, "user", "未登録", "s1")
        conn.execute(
            "INSERT INTO episode_embeddings(episode_id, embedding) VALUES (?, ?)",
            (eid1, pack([0.5] * 768)),
        )
        conn.commit()
    finally:
        conn.close()

    with patch("scripts.upgrade.OllamaClient") as Mock:
        Mock.return_value.embed.return_value = [0.1] * 768
        counts = upgrade(db_path)

    # 未登録の 1 件だけ backfill
    assert counts["episodes_embedded"] == 1


def test_upgrade_inserts_missing_defaults(db_path: Path):
    """default が一つも無い DB に upgrade を走らせると全部 insert."""
    # seed_persona は呼ばない = ペルソナ属性も default も無い空の DB
    with patch("scripts.upgrade.OllamaClient") as Mock:
        Mock.return_value.embed.return_value = []
        counts = upgrade(db_path)

    assert counts["inserted"] == len(DEFAULT_BOOT_FACTS)
    assert counts["updated"] == 0
    assert counts["unchanged"] == 0


def test_upgrade_updates_changed_default(db_path: Path):
    """既存 default の value が古いと、supersede + 新規 insert."""
    with patch("scripts.seed_persona.OllamaClient") as Mock:
        Mock.return_value.embed.return_value = []
        seed(db_path, **_seed_kwargs())

    # response_brevity の値を古いダミーに置き換える (= プラグイン旧版が
    # 入れていた古い文言という想定)
    conn = connect(db_path)
    try:
        conn.execute(
            "UPDATE facts SET value='OLD_VERSION' "
            "WHERE category='persona' AND key='response_brevity' AND status='active'"
        )
        conn.commit()
    finally:
        conn.close()

    with patch("scripts.upgrade.OllamaClient") as Mock:
        Mock.return_value.embed.return_value = []
        counts = upgrade(db_path)

    assert counts["updated"] == 1
    assert counts["inserted"] == 0
    assert counts["unchanged"] == len(DEFAULT_BOOT_FACTS) - 1

    # 新版が active、旧版は superseded、リンクが張られている
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id, value, status, supersedes, superseded_by FROM facts "
            "WHERE category='persona' AND key='response_brevity' ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 2
    old, new = rows
    assert old[1] == "OLD_VERSION"
    assert old[2] == "superseded"
    assert old[4] == new[0]  # superseded_by points to new
    assert new[2] == "active"
    assert new[3] == old[0]  # supersedes points to old


def test_upgrade_does_not_touch_persona_specific_facts(db_path: Path):
    """ペルソナ固有属性 (role/identity/...) は upgrade で触らない."""
    with patch("scripts.seed_persona.OllamaClient") as Mock:
        Mock.return_value.embed.return_value = []
        seed(db_path, **_seed_kwargs())

    conn = connect(db_path)
    try:
        before = conn.execute(
            "SELECT category, key, value FROM facts "
            "WHERE category='persona' AND key IN "
            "  ('role','identity','personality','gender','first_person',"
            "   'speech_style','address_user') "
            "ORDER BY key"
        ).fetchall()
    finally:
        conn.close()

    with patch("scripts.upgrade.OllamaClient") as Mock:
        Mock.return_value.embed.return_value = []
        upgrade(db_path)

    conn = connect(db_path)
    try:
        after = conn.execute(
            "SELECT category, key, value FROM facts "
            "WHERE category='persona' AND key IN "
            "  ('role','identity','personality','gender','first_person',"
            "   'speech_style','address_user') "
            "ORDER BY key"
        ).fetchall()
    finally:
        conn.close()

    assert before == after  # 完全一致 = 触られていない


def test_upgrade_does_not_touch_episodes_and_other_categories(db_path: Path):
    """episodes と preference / aversion / profile / skill / context も無傷."""
    with patch("scripts.seed_persona.OllamaClient") as Mock:
        Mock.return_value.embed.return_value = []
        seed(db_path, **_seed_kwargs())

    # ダミーの user データを足す
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO facts(category, key, value, importance) "
            "VALUES ('preference', 'coffee', '深煎り', 6)"
        )
        conn.execute(
            "INSERT INTO facts(category, key, value, importance) "
            "VALUES ('aversion', 'sweets', '糖尿病で控えてる', 7)"
        )
        conn.execute(
            "INSERT INTO episodes(role, content, session_id) "
            "VALUES ('user', '昨日の話', 's1')"
        )
        conn.commit()
    finally:
        conn.close()

    with patch("scripts.upgrade.OllamaClient") as Mock:
        Mock.return_value.embed.return_value = []
        upgrade(db_path)

    conn = connect(db_path)
    try:
        pref = conn.execute(
            "SELECT value FROM facts WHERE category='preference' AND key='coffee'"
        ).fetchone()
        aver = conn.execute(
            "SELECT value FROM facts WHERE category='aversion' AND key='sweets'"
        ).fetchone()
        ep = conn.execute(
            "SELECT content FROM episodes WHERE session_id='s1'"
        ).fetchone()
    finally:
        conn.close()

    assert pref[0] == "深煎り"
    assert aver[0] == "糖尿病で控えてる"
    assert ep[0] == "昨日の話"


# ── facts CHECK 制約 migration (0.5.25: 'knowledge' category 追加) ─────────

def test_facts_check_migration_adds_knowledge_to_existing_db(tmp_path: Path):
    """0.5.24 以前の DB を再現 → upgrade で 'knowledge' を含む CHECK に migrate される.

    fixture: init_db() で完全初期化した後、 facts table だけ drop して
    0.5.24 以前の旧 CHECK 制約版で再作成する (他テーブル / vec0 は維持).
    """
    db_path = tmp_path / "old.db"
    init_db(db_path)
    conn = connect(db_path)
    try:
        # facts を旧 CHECK 制約 ('knowledge' なし) に差し替え
        conn.execute("DROP TABLE facts")
        conn.execute(
            "CREATE TABLE facts ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  category TEXT NOT NULL CHECK (category IN ("
            "    'persona','rule','preference','aversion','profile','skill','context')),"
            "  key TEXT NOT NULL, value TEXT NOT NULL,"
            "  importance INTEGER NOT NULL CHECK (importance BETWEEN 1 AND 9),"
            "  access_count INTEGER NOT NULL DEFAULT 0,"
            "  status TEXT NOT NULL CHECK (status IN ('active','superseded')) DEFAULT 'active',"
            "  supersedes INTEGER REFERENCES facts(id),"
            "  superseded_by INTEGER REFERENCES facts(id),"
            "  source TEXT,"
            "  created_at TEXT NOT NULL DEFAULT (datetime('now', '+9 hours')),"
            "  updated_at TEXT NOT NULL DEFAULT (datetime('now', '+9 hours')),"
            "  last_accessed_at TEXT"
            ")"
        )
        conn.execute(
            "INSERT INTO facts(category, key, value, importance) VALUES "
            "('preference', 'coffee', '深煎り', 5)"
        )
        conn.commit()
        # 移行前は knowledge を入れようとすると CHECK 違反
        import sqlite3
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO facts(category, key, value, importance) VALUES "
                "('knowledge', 'k_xxx', 'test', 7)"
            )
    finally:
        conn.close()

    # upgrade で migration を走らせる
    with patch("scripts.upgrade.OllamaClient") as Mock:
        Mock.return_value.embed.return_value = []
        counts = upgrade(db_path)
    assert counts["facts_check_migrated"] == 1

    # 既存データは残っている
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT value FROM facts WHERE category='preference' AND key='coffee'"
        ).fetchone()
        assert row[0] == "深煎り"
        # 'knowledge' が INSERT 可能になっている
        conn.execute(
            "INSERT INTO facts(category, key, value, importance) VALUES "
            "('knowledge', 'k_test', 'web 記事の要約', 7)"
        )
        conn.commit()
        n = conn.execute(
            "SELECT COUNT(*) FROM facts WHERE category='knowledge'"
        ).fetchone()[0]
        assert n == 1
    finally:
        conn.close()


def test_facts_check_migration_is_idempotent(db_path: Path):
    """新規 DB (既に 'knowledge' を含む CHECK) では migration は no-op."""
    with patch("scripts.upgrade.OllamaClient") as Mock:
        Mock.return_value.embed.return_value = []
        counts = upgrade(db_path)
    assert counts["facts_check_migrated"] == 0


# ── PERSONA_CORE_KEYS の汚染復元 (0.5.26: ソフィア事案の救済) ───────────────

def test_restore_persona_core_recovers_from_init_seed(db_path: Path):
    """write LLM に汚染された identity/role 等を source='init' から復元する."""
    # seed (= source='init' 入る)
    with patch("scripts.seed_persona.OllamaClient") as Mock:
        Mock.return_value.embed.return_value = []
        seed(db_path, **_seed_kwargs())

    # ソフィア事案を再現: identity / role / address_user を write LLM で上書き
    conn = connect(db_path)
    try:
        # 元 active の identity を superseded に降格 + 汚染 value を新規 insert
        for cat, key, bad_value in [
            ("persona", "identity", "雑談で書かれたゴミ"),
            ("persona", "role", "stitch の障害情報を検索"),
            ("persona", "address_user", "inbox ってなんなの？"),
        ]:
            old = conn.execute(
                "SELECT id FROM facts WHERE category=? AND key=? AND status='active'",
                (cat, key),
            ).fetchone()
            assert old is not None
            conn.execute("UPDATE facts SET status='superseded' WHERE id=?", (old[0],))
            conn.execute(
                "INSERT INTO facts(category, key, value, importance, source) "
                "VALUES (?, ?, ?, 5, 'conversation')",
                (cat, key, bad_value),
            )
        conn.commit()
    finally:
        conn.close()

    # upgrade 実行 (= 復元 step が走る)
    with patch("scripts.upgrade.OllamaClient") as Mock:
        Mock.return_value.embed.return_value = []
        counts = upgrade(db_path)
    assert counts["persona_core_restored"] == 3

    # active fact が init 値に戻っている
    conn = connect(db_path)
    try:
        for cat, key, expected in [
            ("persona", "identity", "このペルソナの名前は『テスト太郎』"),
            ("persona", "role", "バックエンドエンジニアの相棒"),
            ("persona", "address_user", "あなた"),
        ]:
            row = conn.execute(
                "SELECT value, source FROM facts "
                "WHERE category=? AND key=? AND status='active'",
                (cat, key),
            ).fetchone()
            assert row[0] == expected
            assert row[1] == "restore"
    finally:
        conn.close()


def test_restore_persona_core_is_idempotent(db_path: Path):
    """汚染されていない DB では no-op (init と active が一致するので何もしない)."""
    with patch("scripts.seed_persona.OllamaClient") as Mock:
        Mock.return_value.embed.return_value = []
        seed(db_path, **_seed_kwargs())

    with patch("scripts.upgrade.OllamaClient") as Mock:
        Mock.return_value.embed.return_value = []
        counts = upgrade(db_path)
    assert counts["persona_core_restored"] == 0


def test_restore_persona_core_skips_keys_without_init_seed(db_path: Path):
    """source='init' が無い key (= 0.5.0 以前の seed 等) は skip する."""
    with patch("scripts.seed_persona.OllamaClient") as Mock:
        Mock.return_value.embed.return_value = []
        seed(db_path, **_seed_kwargs())

    # source='init' を全部 'manual' に書き換え (init seed が無いケースを再現)
    conn = connect(db_path)
    try:
        conn.execute("UPDATE facts SET source='manual' WHERE source='init'")
        # active な identity を汚染値で上書き
        old = conn.execute(
            "SELECT id FROM facts WHERE category='persona' AND key='identity' AND status='active'"
        ).fetchone()
        conn.execute("UPDATE facts SET status='superseded' WHERE id=?", (old[0],))
        conn.execute(
            "INSERT INTO facts(category, key, value, importance, source) "
            "VALUES ('persona', 'identity', 'ゴミ', 5, 'conversation')"
        )
        conn.commit()
    finally:
        conn.close()

    with patch("scripts.upgrade.OllamaClient") as Mock:
        Mock.return_value.embed.return_value = []
        counts = upgrade(db_path)
    # init seed が無いので復元できない (skip)
    assert counts["persona_core_restored"] == 0


def test_topic_tables_migration_adds_to_existing_db(tmp_path: Path):
    """0.6.23 以前の DB を再現 → upgrade で topic 系テーブル + episodes.topic_id が追加される."""
    db_path = tmp_path / "old.db"
    init_db(db_path)
    conn = connect(db_path)
    try:
        # 旧 DB を再現: topic 系テーブルと topic_id 列を削除
        conn.execute("DROP TABLE IF EXISTS topics")
        conn.execute("DROP TABLE IF EXISTS topic_tags")
        conn.execute("DROP TABLE IF EXISTS topic_relations")
        conn.execute("DROP TABLE IF EXISTS topic_tag_embeddings")
        # episodes.topic_id 列を削除 (旧 DB を模擬)
        conn.execute("CREATE TABLE episodes_old AS SELECT id, role, content, summary, session_id, timestamp FROM episodes")
        conn.execute("DROP TABLE episodes")
        conn.execute(
            "CREATE TABLE episodes ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  role TEXT NOT NULL CHECK (role IN ('user','assistant')),"
            "  content TEXT NOT NULL, summary TEXT,"
            "  session_id TEXT NOT NULL,"
            "  timestamp TEXT NOT NULL DEFAULT (datetime('now', '+9 hours'))"
            ")"
        )
        conn.execute("INSERT INTO episodes SELECT * FROM episodes_old")
        conn.execute("DROP TABLE episodes_old")
        conn.commit()
        # 確認: topic_id 列なし
        cols = {r[1] for r in conn.execute("PRAGMA table_info(episodes)")}
        assert "topic_id" not in cols
    finally:
        conn.close()

    # upgrade 実行
    with patch("scripts.upgrade.OllamaClient") as Mock:
        Mock.return_value.embed.return_value = []
        counts = upgrade(db_path)
    assert counts["topic_tables_added"] == 1
    assert counts["episodes_topic_id_added"] == 1
    assert counts["topic_tag_embeddings_added"] == 1

    # 確認
    conn = connect(db_path)
    try:
        rows = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for t in ("topics", "topic_tags", "topic_relations", "topic_tag_embeddings"):
            assert t in rows
        cols = {r[1] for r in conn.execute("PRAGMA table_info(episodes)")}
        assert "topic_id" in cols
    finally:
        conn.close()


def test_topic_tables_migration_is_idempotent(db_path: Path):
    """新規 DB に対しては no-op."""
    with patch("scripts.upgrade.OllamaClient") as Mock:
        Mock.return_value.embed.return_value = []
        counts = upgrade(db_path)
    assert counts["topic_tables_added"] == 0
    assert counts["episodes_topic_id_added"] == 0
    assert counts["topic_tag_embeddings_added"] == 0
