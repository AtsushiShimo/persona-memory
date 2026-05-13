"""hook 配線ヘルパ.

役割:
- SQLite DB と並んで `<persona>.cozo.db` が存在するか判定
- 存在すれば Cozo recall (流れ再構築) ブロックを生成して返す
- 存在しなければ "" (no-op) — SQLite 経路は無傷

これで /persona-memory:upgrade-cozo を走らせたユーザーだけが新経路を
体験する形 (= incremental rollout. SQLite ユーザーには影響ゼロ).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def cozo_db_path_for(sqlite_db: Path) -> Path:
    """`/path/<persona>.db` → `/path/<persona>.cozo.db`."""
    return sqlite_db.with_suffix(".cozo.db")


def cozo_db_present(sqlite_db: Path) -> bool:
    return cozo_db_path_for(sqlite_db).exists()


def cozo_disabled() -> bool:
    return os.environ.get("PERSONA_COZO_DISABLE", "").strip() == "1"


def maybe_cozo_recall_block(sqlite_db: Path, query: str) -> str:
    """Cozo DB が存在し disable されていなければ流れ再構築ブロックを返す.

    旧来の単発 topic_flow ブロックのみ. recall_full への移行後は
    maybe_cozo_full_recall を使うこと.
    """
    if cozo_disabled() or not cozo_db_present(sqlite_db):
        return ""
    try:
        from scripts.db_cozo.connection import init_db
        from scripts.db_cozo.recall import recall_topic_flow
        from scripts.shared.ollama import OllamaClient
        client = init_db(cozo_db_path_for(sqlite_db))
        llm = OllamaClient()
        emb = llm.embed(
            os.environ.get("PERSONA_EMBED_MODEL", "nomic-embed-text"),
            query,
        )
        if not emb:
            return ""
        return recall_topic_flow(client, emb)
    except Exception as e:
        sys.stderr.write(f"[persona-memory] cozo recall failed: {e}\n")
        return ""


def maybe_cozo_full_recall(
    sqlite_db: Path, query: str, buffer: list[dict] | None = None,
) -> str:
    """Cozo DB が存在し disable されていなければ recall_full を呼んで全部返す.

    旧 SQLite recall を置き換える経路. boot 層の dirty 注入は呼出側で別途.
    """
    if cozo_disabled() or not cozo_db_present(sqlite_db):
        return ""
    try:
        from scripts.db_cozo.connection import init_db
        from scripts.db_cozo.recall_full import recall_full
        from scripts.shared.ollama import OllamaClient
        client = init_db(cozo_db_path_for(sqlite_db))
        return recall_full(client, query, OllamaClient(), buffer=buffer or [])
    except Exception as e:
        sys.stderr.write(f"[persona-memory] cozo full recall failed: {e}\n")
        return ""


def maybe_cozo_topic_shift(
    sqlite_db: Path, session_id: str, new_prompt: str,
) -> str | None:
    """Cozo DB が存在し disable されていなければ話題シフト判定 → 必要なら topic 切替.

    戻り値: 新 topic_id (切替時) or 既存 topic_id (継続) or None (bypass).
    """
    if cozo_disabled() or not cozo_db_present(sqlite_db):
        return None
    try:
        from scripts.db_cozo.connection import init_db
        from scripts.db_cozo.topic_shift import maybe_split_topic
        from scripts.shared.ollama import OllamaClient
        client = init_db(cozo_db_path_for(sqlite_db))
        topic_id, _judgment = maybe_split_topic(
            client, session_id, new_prompt, OllamaClient(),
        )
        return topic_id
    except Exception as e:
        sys.stderr.write(f"[persona-memory] cozo topic shift failed: {e}\n")
        return None


def maybe_cozo_save_episode(
    sqlite_db: Path, role: str, content: str, session_id: str,
) -> int | None:
    """Cozo 側にも episode を保存. Cozo DB 不在なら no-op.

    旧 SQLite 経路は呼出側で実行済みなのでこれは追加保存. 戻り値は
    Cozo 側の episode_id (= SQLite と独立採番).
    """
    if cozo_disabled() or not cozo_db_present(sqlite_db):
        return None
    try:
        from scripts.db_cozo.connection import init_db
        from scripts.db_cozo.repo import save_episode as cozo_save
        client = init_db(cozo_db_path_for(sqlite_db))
        return cozo_save(client, role=role, content=content, session_id=session_id)
    except Exception as e:
        sys.stderr.write(f"[persona-memory] cozo save_episode failed: {e}\n")
        return None
