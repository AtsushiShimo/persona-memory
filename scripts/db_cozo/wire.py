"""hook 配線ヘルパ.

役割:
- `<persona>.cozo.db` が存在するか判定 (= ペルソナ init 済か)
- 存在すれば Cozo recall (流れ再構築 / topic / graph) ブロックを生成して返す
- 存在しなければ "" (no-op) — init 未実行扱い

0.8.0 で業務フローを Cozo only 化、 0.8.2 で SQLite migrate 経路も廃止.
旧 SQLite path (PERSONA_MEMORY_DB env が `<persona>.db`) を受けた caller の
ために `cozo_db_path_for` で `.cozo.db` に振り直す path-only 救済のみ残置.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def cozo_db_path_for(db_path: Path) -> Path:
    """`<persona>.db` (旧 SQLite) または `<persona>.cozo.db` を `.cozo.db` に正規化.

    冪等: 既に `.cozo.db` で終わるパスはそのまま返す. これにより新規 caller が
    `<persona>.cozo.db` を直接渡しても `.cozo.cozo.db` のような二重 suffix
    に膨れないことを保証する.
    """
    if db_path.name.endswith(".cozo.db"):
        return db_path
    return db_path.with_suffix(".cozo.db")


def cozo_db_present(db_path: Path) -> bool:
    return cozo_db_path_for(db_path).exists()


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
    """[DEPRECATED 0.7.3] 旧 topic_shift 経路の互換シム.

    0.7.3 で「生きてる話題箱」 方式 (`maybe_cozo_identify_topic`) に統合され
    包摂された. 旧呼び出しを残しつつ新経路に委譲する.
    """
    return maybe_cozo_identify_topic(sqlite_db, session_id, new_prompt, role="user")


def maybe_cozo_identify_topic(
    sqlite_db: Path,
    session_id: str,
    content: str,
    role: str = "user",
) -> str | None:
    """0.7.3 「生きてる話題箱」 方式の topic 同定 + active topic 切替.

    new prompt を embedding 化 → alive topic 群と summary embedding で並列照合.
      - 閾値内に match → その topic を active にし、 summary を更新
      - match なし → 新 topic を発行し初期 summary を生成

    戻り値: 確定 active topic_id. DB 不在 / 無効化時 None.

    重さ: embedding 1-2 回 + LLM 1 回 (summary 更新 / 生成). hook 同期で
    呼ぶことを前提に最小限の LLM コール構成.
    """
    if cozo_disabled() or not cozo_db_present(sqlite_db):
        return None
    try:
        from scripts.db_cozo.connection import init_db
        from scripts.db_cozo.topic_identify import identify_topic
        from scripts.shared.ollama import OllamaClient
        client = init_db(cozo_db_path_for(sqlite_db))
        result = identify_topic(
            client, role=role, content=content,
            session_id=session_id, llm=OllamaClient(),
        )
        return result.topic_id
    except Exception as e:
        sys.stderr.write(f"[persona-memory] cozo identify_topic failed: {e}\n")
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


def maybe_cozo_extract_graph(
    sqlite_db: Path,
    role: str,
    content: str,
    session_id: str,
    buffer: list[dict] | None = None,
    episode_id: int | None = None,
) -> tuple[int | None, str | None]:
    """新規発話から議論ノード + (任意で) 直前ノードへのエッジを生成して Cozo に保存.

    リアルタイム経路の本丸. 旧 SQLite 側の `scripts.discussion.graph` 経路と独立に
    Cozo の discussion_node / discussion_edge を育てるための配線.

    手順 (0.7.3 改):
      1. Cozo DB 存在チェック
      2. session の active topic を取得 (user 発話側 hook で identify 済みの想定)
         - assistant 発話の場合は同じ topic 内で summary を update_summary で追記更新
      3. topic 内の直前ノードを get_last_node_in_topic
      4. graph_extract.extract_node_with_relation で LLM 抽出 (prev_relation 込み)
      5. ノード抽出成功 → embedding (title+content) を計算 → add_node で保存
      6. prev_relation 非 null かつ直前ノード存在 → add_edge

    戻り値: (node_id, edge_kind). LLM 抽出失敗時 or DB 不在時は (None, None).
    fail-open: 例外は飲み込んで (None, None) を返す (本処理 = 発話応答を妨げない).
    """
    if cozo_disabled() or not cozo_db_present(sqlite_db):
        return None, None
    try:
        from scripts.db_cozo.connection import init_db
        from scripts.db_cozo.discussion import (
            add_edge, add_node, get_last_node_in_topic,
            get_recent_nodes_in_topic,
        )
        from scripts.db_cozo.graph_extract import extract_node_with_relation
        from scripts.db_cozo.repo import (
            get_active_topic, get_topic_summary_emb,
            touch_topic, upsert_topic_summary_emb,
        )
        from scripts.db_cozo.topic_summary import update_summary
        from scripts.shared.embedding import truncate_for_embedding
        from scripts.shared.ollama import OllamaClient
        client = init_db(cozo_db_path_for(sqlite_db))
        llm = OllamaClient()
        topic_id = get_active_topic(client, session_id)
        # assistant 発話側で summary を追記更新 (user 側は hook で同期更新済み).
        # role に関わらず touch_topic で「生きてる」 を維持.
        if role == "assistant" and topic_id:
            try:
                cur = get_topic_summary_emb(client, topic_id)
                old_sum = (cur or {}).get("summary") or ""
                new_sum = update_summary(old_sum, role, content, llm)
                if new_sum and new_sum != old_sum:
                    try:
                        sum_emb = llm.embed(
                            os.environ.get("PERSONA_EMBED_MODEL", "nomic-embed-text"),
                            new_sum,
                        ) or []
                    except Exception:
                        sum_emb = []
                    upsert_topic_summary_emb(client, topic_id, new_sum, sum_emb)
            except Exception as e:
                sys.stderr.write(
                    f"[persona-memory] cozo assistant summary update failed: {e}\n"
                )
        if topic_id:
            try:
                touch_topic(client, topic_id)
            except Exception:
                pass
        prev = get_last_node_in_topic(client, topic_id) if topic_id else None
        # 0.7.3: 同 topic 内の最近 N ノードを候補として LLM に渡す (離れた edge 用).
        recent_nodes = (
            get_recent_nodes_in_topic(client, topic_id, limit=10)
            if topic_id else []
        )
        # 直前ノードは別途渡しているので候補からは除外
        if prev:
            recent_nodes = [n for n in recent_nodes if n["id"] != prev["id"]]
        nc = extract_node_with_relation(
            role, content, buffer or [], llm,
            prev_node=prev, candidate_nodes=recent_nodes,
        )
        if nc is None:
            return None, None
        emb_text = nc.title
        if nc.content:
            emb_text = f"{nc.title}\n{nc.content}"
        emb_text = truncate_for_embedding(emb_text)
        embedding: list[float] = []
        try:
            embedding = llm.embed(
                os.environ.get("PERSONA_EMBED_MODEL", "nomic-embed-text"),
                emb_text,
            ) or []
        except Exception:
            embedding = []
        nid = add_node(
            client,
            kind=nc.kind,
            title=nc.title,
            state=nc.state,
            content=nc.content,
            episode_id=episode_id,
            embedding=embedding if embedding else None,
        )
        edge_kind: str | None = None
        # 直前ノードへの edge (旧来通り)
        if nc.prev_relation and prev and prev["id"] != nid:
            add_edge(client, prev["id"], nid, nc.prev_relation)
            edge_kind = nc.prev_relation
        # 0.7.3: 離れたノードへの edge (LLM が指定した target_id)
        if nc.target_id and nc.target_relation and nc.target_id != nid:
            # 候補リストに含まれる id のみ受け付ける (LLM 幻覚防止)
            valid_ids = {n["id"] for n in recent_nodes}
            if prev:
                valid_ids.add(prev["id"])
            if nc.target_id in valid_ids:
                try:
                    add_edge(client, nc.target_id, nid, nc.target_relation)
                except Exception as e:
                    sys.stderr.write(
                        f"[persona-memory] target edge add failed: {e}\n"
                    )
        return nid, edge_kind
    except Exception as e:
        sys.stderr.write(f"[persona-memory] cozo extract_graph failed: {e}\n")
        return None, None
