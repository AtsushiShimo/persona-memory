#!/usr/bin/env python3
"""プラグイン更新後の non-destructive な boot 層 default refresh + episode 埋め込み backfill.

`/persona-memory:upgrade` から呼ばれる。idempotent:
- DEFAULT_BOOT_FACTS の各 (category, key) が DB に無ければ追加
- 既存があり value が一致 → 何もしない
- 既存があり value が異なる → 旧版を superseded に降格、新版を insert
  + supersedes / superseded_by 双方向リンクを張る
- ペルソナ固有属性 (role/name/personality/gender/first_person/
  speech_style/address_user) には触らない
- episodes / 他 category facts も無傷
- **episode_embeddings に未登録の episode を backfill** (recall 時の
  ベクトル検索の対象にするため)

ユーザーが運用中に蓄積した会話・好み・知識を一切損なわず、
共通の振る舞いルールだけを最新に保つ。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.boot.defaults import (
    DEFAULT_BOOT_FACTS,
    DEPRECATED_BOOT_FACTS,
    PERSONA_CORE_KEYS,
)
from scripts.db.connection import connect
from scripts.shared.embedding import pack
from scripts.shared.ollama import OllamaClient

EMBED_MODEL = os.environ.get("PERSONA_EMBED_MODEL", "nomic-embed-text")


def _backfill_episode_embeddings(conn, client) -> int:
    """episode_embeddings に未登録の episodes を embedding 化して埋める.

    戻り値: backfill した件数. episode_embeddings table が存在しない場合は 0.
    """
    # 古い DB / 不完全な fixture で table が無い場合は安全に skip
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='episode_embeddings'"
    ).fetchone():
        return 0
    rows = conn.execute(
        """
        SELECT e.id, e.content
        FROM episodes e
        LEFT JOIN episode_embeddings ee ON ee.episode_id = e.id
        WHERE ee.episode_id IS NULL
          AND e.content IS NOT NULL AND e.content != ''
        ORDER BY e.id
        """
    ).fetchall()
    n = 0
    for ep_id, content in rows:
        try:
            vec = client.embed(EMBED_MODEL, content)
            if not vec:
                continue
            conn.execute(
                "INSERT OR REPLACE INTO episode_embeddings(episode_id, embedding) VALUES (?, ?)",
                (ep_id, pack(vec)),
            )
            conn.commit()
            n += 1
        except Exception as e:
            print(f"  WARN backfill embedding failed (episode_id={ep_id}): {e}", file=sys.stderr)
    return n


def _purge_superseded_embeddings(conn) -> int:
    """superseded fact の embedding を fact_embeddings から削除する.

    旧 supersede() は embedding を残していたため、recall の vec0 knn で
    active 外の古い embedding が枠を喰い、関連 fact が漏れる現象があった。
    新しい supersede() は削除するが、過去に蓄積された残骸を一括クリーン。
    戻り値: 削除した件数. fact_embeddings table が無い場合は 0.
    """
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='fact_embeddings'"
    ).fetchone():
        return 0
    cur = conn.execute(
        "DELETE FROM fact_embeddings WHERE fact_id IN ("
        "  SELECT id FROM facts WHERE status='superseded'"
        ")"
    )
    conn.commit()
    return cur.rowcount or 0


def _refresh_fact_embeddings(conn, client) -> int:
    """全 active facts の embedding を `<category>/<key>: <value>` 形式で再生成する.

    write 経路が以前 value 単独で embed していたため、短い fact value
    (例: 「糖尿病」「まろん」) は nomic-embed-text の OOV collapse で
    無関係な fact と cosine 距離 0 で衝突していた。新フォーマットで
    全 fact を統一すると衝突が解消する。idempotent (何度走らせても同結果)。

    戻り値: 再生成した件数. fact_embeddings table が無い場合は 0.
    """
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='fact_embeddings'"
    ).fetchone():
        return 0
    rows = conn.execute(
        "SELECT id, category, key, value FROM facts "
        "WHERE status='active' ORDER BY id"
    ).fetchall()
    n = 0
    for fid, cat, key, val in rows:
        try:
            vec = client.embed(EMBED_MODEL, f"{cat}/{key}: {val}")
            if not vec:
                continue
            # vec0 は INSERT OR REPLACE をサポートしないため DELETE + INSERT で上書き
            conn.execute("DELETE FROM fact_embeddings WHERE fact_id=?", (fid,))
            conn.execute(
                "INSERT INTO fact_embeddings(fact_id, embedding) VALUES (?, ?)",
                (fid, pack(vec)),
            )
            conn.commit()
            n += 1
        except Exception as e:
            print(
                f"  WARN refresh fact embedding failed (fact_id={fid}): {e}",
                file=sys.stderr,
            )
    return n


def _migrate_facts_check_constraint(conn) -> bool:
    """既存 facts テーブルの CHECK 制約に 'knowledge' が無ければ追加する.

    SQLite は ALTER TABLE で CHECK を変更できないため、 facts_new を作って
    データを移し、 旧 facts を drop して RENAME する。 idempotent — 既に
    'knowledge' が含まれている DB では何もしない。
    戻り値: 実際に再作成した場合 True.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='facts'"
    ).fetchone()
    if not row:
        return False
    if "'knowledge'" in row[0]:
        return False  # already migrated

    # facts 再作成中は FK チェックを一時 OFF (conflicts が facts(id) を参照しているため)
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute(
            "CREATE TABLE facts_new ("
            "  id                INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  category          TEXT NOT NULL CHECK (category IN ("
            "    'persona','rule','preference','aversion','profile',"
            "    'skill','context','knowledge')),"
            "  key               TEXT NOT NULL,"
            "  value             TEXT NOT NULL,"
            "  importance        INTEGER NOT NULL CHECK (importance BETWEEN 1 AND 9),"
            "  access_count      INTEGER NOT NULL DEFAULT 0,"
            "  status            TEXT NOT NULL CHECK (status IN ('active','superseded')) DEFAULT 'active',"
            "  supersedes        INTEGER REFERENCES facts(id),"
            "  superseded_by     INTEGER REFERENCES facts(id),"
            "  source            TEXT,"
            "  created_at        TEXT NOT NULL DEFAULT (datetime('now', '+9 hours')),"
            "  updated_at        TEXT NOT NULL DEFAULT (datetime('now', '+9 hours')),"
            "  last_accessed_at  TEXT"
            ")"
        )
        conn.execute(
            "INSERT INTO facts_new "
            "SELECT id, category, key, value, importance, access_count, status, "
            "       supersedes, superseded_by, source, created_at, updated_at, "
            "       last_accessed_at "
            "FROM facts"
        )
        conn.execute("DROP TABLE facts")
        conn.execute("ALTER TABLE facts_new RENAME TO facts")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_facts_active_unique "
            "ON facts(category, key) WHERE status = 'active'"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_facts_category   ON facts(category)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_facts_status     ON facts(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_facts_supersedes ON facts(supersedes)")
        conn.commit()
        return True
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


def _restore_persona_core_from_init(conn, client) -> int:
    """PERSONA_CORE_KEYS が write LLM で汚染されていた場合、 source='init' の
    最古 fact から復元する.

    0.5.25 以前は persona/identity 等の核属性が write LLM 上書き保護対象外で、
    雑談の発話が JSON 抽出で `persona/identity` 値として登録されると簡単に
    上書きされてしまった (ソフィア事案で 14 段 supersede された identity を
    観測). 0.5.26 の PROTECTED_KEYS 拡張で予防は塞がるが、 既存汚染は
    自動修復する必要があるため本関数を追加.

    手順:
    - 各 PERSONA_CORE_KEY について active fact を取得
    - 同 key で source='init' の最古 fact を取得
    - active.value != init.value なら、 active を superseded に降格、
      init.value で新規 INSERT (source='restore') + embedding 再生成
    - source='init' の fact が存在しない (= 0.5.0 以前の seed 等) なら skip

    idempotent: active がすでに init と一致していれば no-op.
    戻り値: 復元した key 数.
    """
    restored = 0
    for cat, key in PERSONA_CORE_KEYS:
        active_row = conn.execute(
            "SELECT id, value FROM facts "
            "WHERE category=? AND key=? AND status='active'",
            (cat, key),
        ).fetchone()
        if not active_row:
            continue  # そもそも seed されていない key
        init_row = conn.execute(
            "SELECT id, value, importance FROM facts "
            "WHERE category=? AND key=? AND source='init' "
            "ORDER BY id ASC LIMIT 1",
            (cat, key),
        ).fetchone()
        if not init_row:
            continue  # init seed が無いペルソナ (0.5.0 以前等)
        active_id, active_value = active_row
        init_id, init_value, init_importance = init_row
        if active_value == init_value:
            continue  # 既に正しい値 (idempotent)

        # 現 active を superseded に降格
        conn.execute(
            "UPDATE facts SET status='superseded', "
            "  updated_at=datetime('now', '+9 hours') "
            "WHERE id=?",
            (active_id,),
        )
        # init value で新規 active を INSERT (source='restore' で由来を残す)
        cur = conn.execute(
            "INSERT INTO facts(category, key, value, importance, source, supersedes) "
            "VALUES (?, ?, ?, ?, 'restore', ?)",
            (cat, key, init_value, init_importance, active_id),
        )
        new_id = cur.lastrowid
        conn.execute(
            "UPDATE facts SET superseded_by=? WHERE id=?",
            (new_id, active_id),
        )
        # 旧 embedding を削除して新 value で再生成
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='fact_embeddings'"
        ).fetchone():
            conn.execute("DELETE FROM fact_embeddings WHERE fact_id=?", (active_id,))
            try:
                vec = client.embed(EMBED_MODEL, f"{cat}/{key}: {init_value}")
                if vec:
                    conn.execute(
                        "INSERT OR REPLACE INTO fact_embeddings(fact_id, embedding) "
                        "VALUES (?, ?)",
                        (new_id, pack(vec)),
                    )
            except Exception as e:
                print(
                    f"  WARN restore embed failed [{cat}/{key}]: {e}",
                    file=sys.stderr,
                )
        conn.commit()
        restored += 1
        print(f"  restored [{cat}/{key}] -> {init_value[:50]!r}")
    return restored


def _ensure_episodes_fts(conn) -> bool:
    """既存 DB に episodes_fts (FTS5 trigram) + 同期 trigger を追加 (0.6.0 phase3).

    vec0 cosine の弱点 (短文 / 固有名詞 / typo) を BM25 で補うための
    補完検索経路. 新規 DB は schema.sql で作成済み. ここでは既存 DB に
    後追いで作って既存 episodes 全件を populate する. idempotent.

    戻り値: 実際に新規作成した場合 True.
    """
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='episodes_fts'"
    ).fetchone()
    if exists:
        return False
    # episodes 本体テーブルが無い (= ほぼあり得ないが念のため) なら何もしない
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='episodes'"
    ).fetchone():
        return False
    conn.executescript(
        """
        CREATE VIRTUAL TABLE episodes_fts USING fts5(
          content,
          content='episodes',
          content_rowid='id',
          tokenize='trigram'
        );
        CREATE TRIGGER episodes_ai AFTER INSERT ON episodes BEGIN
          INSERT INTO episodes_fts(rowid, content) VALUES (new.id, new.content);
        END;
        CREATE TRIGGER episodes_ad AFTER DELETE ON episodes BEGIN
          INSERT INTO episodes_fts(episodes_fts, rowid, content)
            VALUES('delete', old.id, old.content);
        END;
        CREATE TRIGGER episodes_au AFTER UPDATE ON episodes BEGIN
          INSERT INTO episodes_fts(episodes_fts, rowid, content)
            VALUES('delete', old.id, old.content);
          INSERT INTO episodes_fts(rowid, content) VALUES (new.id, new.content);
        END;
        """
    )
    # 既存 episodes を一括 populate
    conn.execute("INSERT INTO episodes_fts(episodes_fts) VALUES('rebuild')")
    conn.commit()
    return True


def _ensure_lint_tables(conn) -> bool:
    """v0.5.12 追加の conflicts / lint_log を既存 DB に migrate.

    schema.sql は CREATE TABLE IF NOT EXISTS なので idempotent.
    戻り値: いずれか新規 create された場合 True.
    """
    schema_file = Path(__file__).resolve().parent / "db" / "schema.sql"
    text = schema_file.read_text(encoding="utf-8")
    # 該当 statement だけ抜き出して実行 (全 schema 流すと既存テーブル定義の差分
    # が出る可能性があるため, 新規テーブル分のみ idempotent に投げる).
    snippets = []
    capture = False
    buf: list[str] = []
    for stmt in text.split(";"):
        s = stmt.strip()
        if not s:
            continue
        target = (
            "conflicts" in s and "CREATE TABLE" in s
        ) or (
            "lint_log" in s and "CREATE TABLE" in s
        ) or (
            "idx_conflicts_" in s and "CREATE INDEX" in s
        ) or (
            "idx_lint_log_" in s and "CREATE INDEX" in s
        )
        if target:
            snippets.append(s + ";")
    before = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master "
        "WHERE type='table' AND name IN ('conflicts','lint_log')"
    ).fetchone()[0]
    for s in snippets:
        conn.execute(s)
    conn.commit()
    after = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master "
        "WHERE type='table' AND name IN ('conflicts','lint_log')"
    ).fetchone()[0]
    return after > before


def _migrate_config_env_to_relative(db_path: Path) -> str:
    """config.env の PERSONA_MEMORY_DB を絶対パスから ${CLAUDE_PROJECT_DIR} 基準に書き換える.

    旧 init.sh / new-persona.sh は絶対パスで書いていたため、プロジェクトディレクトリを
    コピー・移動すると古い場所を指したまま壊れる。新形式 (literal ${CLAUDE_PROJECT_DIR})
    に書き換えて移動耐性を確保する。

    戻り値:
      "migrated" — 旧形式を検出して書き換えた
      "already"  — 既に新形式
      "skipped"  — config.env が無い、または PERSONA_MEMORY_DB 行が無い
    """
    persona = db_path.stem
    config_env = db_path.parent / f"{persona}.config.env"
    if not config_env.exists():
        return "skipped"
    try:
        text = config_env.read_text(encoding="utf-8")
    except Exception:
        return "skipped"

    lines = text.splitlines(keepends=True)
    new_value = (
        f'PERSONA_MEMORY_DB="${{CLAUDE_PROJECT_DIR:-$(pwd)}}/'
        f'.persona-memory/{persona}.db"'
    )
    changed = False
    found = False
    out_lines = []
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith("PERSONA_MEMORY_DB="):
            found = True
            # 既に literal ${CLAUDE_PROJECT_DIR} が含まれていれば新形式
            if "${CLAUDE_PROJECT_DIR" in stripped:
                out_lines.append(line)
                continue
            # 旧形式 (絶対パス等) → 書き換え
            newline_char = "\n" if line.endswith("\n") else ""
            out_lines.append(new_value + newline_char)
            changed = True
        else:
            out_lines.append(line)

    if not found:
        return "skipped"
    if not changed:
        return "already"

    # backup + atomic write
    backup = config_env.with_suffix(config_env.suffix + ".bak")
    try:
        backup.write_text(text, encoding="utf-8")
    except Exception:
        pass
    config_env.write_text("".join(out_lines), encoding="utf-8")
    return "migrated"


def upgrade(db_path: Path) -> dict[str, int]:
    """戻り値: 各操作のカウント."""
    counts = {
        "inserted": 0, "updated": 0, "unchanged": 0,
        "deprecated": 0, "episodes_embedded": 0,
        "facts_re_embedded": 0, "superseded_purged": 0,
        "lint_tables_added": 0,
        "facts_check_migrated": 0,
        "persona_core_restored": 0,
        "episodes_fts_built": 0,
    }
    conn = connect(db_path)
    client = OllamaClient()
    try:
        if _migrate_facts_check_constraint(conn):
            counts["facts_check_migrated"] = 1
            print("  migrated facts CHECK constraint (added 'knowledge' category)")
        if _ensure_episodes_fts(conn):
            counts["episodes_fts_built"] = 1
            n_pop = conn.execute("SELECT COUNT(*) FROM episodes_fts").fetchone()[0]
            print(f"  built episodes_fts (FTS5 trigram), populated {n_pop} rows")
        if _ensure_lint_tables(conn):
            counts["lint_tables_added"] = 1
            print("  added lint tables (conflicts, lint_log)")
        # 1. 廃止された default を active → superseded に降格
        for category, key in DEPRECATED_BOOT_FACTS:
            row = conn.execute(
                "SELECT id FROM facts "
                "WHERE category=? AND key=? AND status='active'",
                (category, key),
            ).fetchone()
            if row is None:
                continue
            conn.execute(
                "UPDATE facts SET status='superseded', "
                "  updated_at=datetime('now', '+9 hours') "
                "WHERE id=?",
                (row[0],),
            )
            conn.commit()
            counts["deprecated"] += 1
            print(f"  deprecated [{category}/{key}]")
        # 2. 現行 default を idempotent に refresh
        for category, key, value, importance in DEFAULT_BOOT_FACTS:
            row = conn.execute(
                "SELECT id, value, importance FROM facts "
                "WHERE category=? AND key=? AND status='active'",
                (category, key),
            ).fetchone()

            if row and row[1] == value and row[2] == importance:
                counts["unchanged"] += 1
                continue

            old_id = row[0] if row else None

            if old_id is not None:
                conn.execute(
                    "UPDATE facts SET status='superseded', "
                    "  updated_at=datetime('now', '+9 hours') "
                    "WHERE id=?",
                    (old_id,),
                )

            cur = conn.execute(
                "INSERT INTO facts(category, key, value, importance, source, supersedes) "
                "VALUES (?, ?, ?, ?, 'upgrade', ?)",
                (category, key, value, importance, old_id),
            )
            new_id = cur.lastrowid

            if old_id is not None:
                conn.execute(
                    "UPDATE facts SET superseded_by=? WHERE id=?",
                    (new_id, old_id),
                )
                counts["updated"] += 1
            else:
                counts["inserted"] += 1

            try:
                vec = client.embed(EMBED_MODEL, f"{category}/{key}: {value}")
                if vec:
                    conn.execute(
                        "INSERT OR REPLACE INTO fact_embeddings(fact_id, embedding) "
                        "VALUES (?, ?)",
                        (new_id, pack(vec)),
                    )
            except Exception as e:
                print(
                    f"  WARN [{category}/{key}] embedded skipped: {e}",
                    file=sys.stderr,
                )

            conn.commit()
            label = "inserted" if old_id is None else "updated"
            print(f"  {label} [{category}/{key}]")

        # 3. episodes embedding backfill (recall 時のベクトル検索のため)
        n = _backfill_episode_embeddings(conn, client)
        counts["episodes_embedded"] = n
        if n > 0:
            print(f"  backfilled embeddings for {n} episode(s)")

        # 4. superseded fact の embedding を一括削除
        #    (旧 supersede() が残していた残骸を消す。recall の vec0 knn 枠を
        #    active 外の古い embedding が喰う問題を解消)
        purged = _purge_superseded_embeddings(conn)
        counts["superseded_purged"] = purged
        if purged > 0:
            print(f"  purged {purged} stale embedding(s) of superseded facts")

        # 5. fact_embeddings を新フォーマット `<category>/<key>: <value>`
        #    で全件再生成 (旧 write 経路で value 単独 embed されていた fact が
        #    OOV collapse で衝突していたため一斉に修復)
        m = _refresh_fact_embeddings(conn, client)
        counts["facts_re_embedded"] = m
        if m > 0:
            print(f"  re-embedded {m} fact(s) with category/key/value format")

        # 6. PERSONA_CORE_KEYS の汚染を init seed 値から復元
        #    (0.5.25 以前で role / identity / address_user 等が write LLM に
        #    上書きされてしまったペルソナを救済. 0.5.26 で予防 + 救済)
        r = _restore_persona_core_from_init(conn, client)
        counts["persona_core_restored"] = r
        if r > 0:
            print(f"  restored {r} persona-core fact(s) from init seed")

    finally:
        conn.close()
    return counts


def main() -> None:
    p = argparse.ArgumentParser(
        description="Refresh default boot facts (non-destructive)."
    )
    p.add_argument(
        "--db", type=Path, default=None,
        help="DB ファイルパス (未指定時は PERSONA_MEMORY_DB env を使う)",
    )
    args = p.parse_args()

    db_path = args.db
    if db_path is None:
        env_db = os.environ.get("PERSONA_MEMORY_DB", "").strip()
        if not env_db:
            print(
                "ERROR: --db か PERSONA_MEMORY_DB env のどちらかが必要です",
                file=sys.stderr,
            )
            sys.exit(2)
        db_path = Path(env_db)

    if not db_path.exists():
        print(f"ERROR: DB が存在しません: {db_path}", file=sys.stderr)
        sys.exit(2)

    counts = upgrade(db_path)

    # config.env を ${CLAUDE_PROJECT_DIR} 基準の相対形式に migrate
    # (旧 init.sh / new-persona.sh が絶対パスで書いていた config.env を救済)
    cfg_result = _migrate_config_env_to_relative(db_path)
    if cfg_result == "migrated":
        print("  migrated config.env PERSONA_MEMORY_DB → ${CLAUDE_PROJECT_DIR} 相対形式")
    elif cfg_result == "already":
        print("  config.env は既に相対形式 (skip)")

    print()
    print(
        f"upgrade summary: inserted={counts['inserted']}, "
        f"updated={counts['updated']}, unchanged={counts['unchanged']}, "
        f"deprecated={counts['deprecated']}, "
        f"episodes_embedded={counts['episodes_embedded']}, "
        f"superseded_purged={counts['superseded_purged']}, "
        f"facts_re_embedded={counts['facts_re_embedded']}, "
        f"lint_tables_added={counts['lint_tables_added']}, "
        f"facts_check_migrated={counts['facts_check_migrated']}, "
        f"persona_core_restored={counts['persona_core_restored']}, "
        f"episodes_fts_built={counts['episodes_fts_built']}"
    )


if __name__ == "__main__":
    main()
