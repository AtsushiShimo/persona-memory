"""Persona memory MCP server (stdio transport).

Tools exposed:
  - write_fact         upsert a structured memory fact + embedding
  - search_memory      vector search over facts (and optionally episodes)
  - lint_memory        detect contradicting fact pairs and flag/auto-resolve
  - append_episode     append a free-form episode (e.g., session summary)
  - list_facts         debug: list active facts
  - health_check       Ollama + DB reachability
"""
from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from . import db
from .embedding import (
    EmbeddingError,
    embed_text,
    health,
    judge_conflict,
    l2_normalize,
)

mcp = FastMCP("persona-memory")

LINT_AUTO_RESOLVE_THRESHOLD = 90
LINT_FLAG_THRESHOLD = 60
# L2 distance on L2-normalized embeddings: d^2 = 2*(1 - cosine_sim).
# 0.7 corresponds to cosine_sim >= ~0.755 (semantically close pairs).
NEIGHBOR_DISTANCE_MAX = 0.7


@mcp.tool()
async def write_fact(
    category: str,
    key: str,
    value: str,
    importance: int = 5,
    source: str | None = None,
) -> dict[str, Any]:
    """Upsert a structured fact and its embedding.

    Args:
      category: e.g. "preference" / "rule" / "profile" / "skill"
      key: stable identifier within the category (e.g. "preferred_language")
      value: free-form text describing the fact
      importance: 1-10 (default 5). Higher = more likely to be injected.
      source: optional provenance (e.g. "user-statement", "session:abc")
    """
    if not (1 <= importance <= 10):
        return {"error": "importance must be between 1 and 10"}
    fact_id = db.upsert_fact(
        category=category,
        key=key,
        value=value,
        importance=importance,
        source=source,
    )
    try:
        vec = await embed_text(f"{category}/{key}: {value}")
    except EmbeddingError as e:
        return {
            "fact_id": fact_id,
            "embedded": False,
            "warning": str(e),
        }
    db.write_fact_embedding(fact_id, l2_normalize(vec))
    return {"fact_id": fact_id, "embedded": True, "embedding_dim": len(vec)}


@mcp.tool()
async def save_knowledge(
    source_url: str,
    title: str,
    summary: str,
    importance: int = 7,
) -> dict[str, Any]:
    """Save external knowledge (web research results) as a structured fact.

    Called by the main agent after web research (WebFetch / web-page-reader /
    x-post-reader skills etc.) to persist the findings as long-lived knowledge.
    Same URL re-research overwrites the previous entry (last-write-wins);
    different URLs about the same topic coexist as separate entries.

    Args:
      source_url: URL of the source (used to derive a stable key and stored
        as the fact's source provenance).
      title: short title of the article/page.
      summary: 2-5 sentence summary of the key takeaways.
      importance: 1-10 (default 7; higher than typical preferences/skills
        because external knowledge is long-lived).
    """
    if not (1 <= importance <= 10):
        return {"error": "importance must be between 1 and 10"}
    if not source_url:
        return {"error": "source_url is required"}
    if not title:
        return {"error": "title is required"}
    if not summary:
        return {"error": "summary is required"}

    import hashlib
    url_hash = hashlib.sha1(source_url.encode("utf-8")).hexdigest()[:10]
    key = f"k_{url_hash}"
    value = f"{title}\n{summary}\nsource: {source_url}"

    fact_id = db.upsert_fact(
        category="knowledge",
        key=key,
        value=value,
        importance=importance,
        source=source_url,
    )
    try:
        vec = await embed_text(f"knowledge/{key}: {value}")
    except EmbeddingError as e:
        return {
            "fact_id": fact_id,
            "key": key,
            "embedded": False,
            "warning": str(e),
        }
    db.write_fact_embedding(fact_id, l2_normalize(vec))
    return {
        "fact_id": fact_id,
        "key": key,
        "embedded": True,
        "embedding_dim": len(vec),
    }


@mcp.tool()
async def search_memory(
    query: str,
    top_k: int = 5,
    category: str | None = None,
    include_episodes: bool = True,
) -> dict[str, Any]:
    """Vector search over facts and (by default) episodes.

    Returns the top_k nearest facts to the query. If include_episodes is True
    (default since 0.5.29), also returns matching episode summaries — this is
    the normal case for the main agent recalling past discussion content, e.g.
    'what did we decide last session?'. Set False only when you specifically
    want curated facts and not raw conversation log.
    """
    try:
        vec = await embed_text(query)
    except EmbeddingError as e:
        return {"error": str(e)}
    norm_vec = l2_normalize(vec)
    out: dict[str, Any] = {
        "facts": db.search_facts(
            embedding=norm_vec, top_k=top_k, category=category
        ),
    }
    if include_episodes:
        out["episodes"] = db.search_episodes(embedding=norm_vec, top_k=top_k)
    return out


@mcp.tool()
async def lint_memory(neighbor_top_k: int = 5) -> dict[str, Any]:
    """Detect contradicting fact pairs.

    For each active fact, find the nearest neighbors (cosine distance below
    NEIGHBOR_DISTANCE_MAX), ask the judge model whether they contradict, and
    classify by confidence:
      >= 90  -> auto_superseded (older fact marked superseded)
      60-89  -> flagged as 'conflict' (human review needed)
      <  60  -> ignored (judged not contradicting strongly)
    """
    facts = db.list_active_facts()
    if len(facts) < 2:
        db.record_lint_run(
            pairs_examined=0, conflicts_flagged=0, conflicts_auto_resolved=0
        )
        return {"pairs_examined": 0, "flagged": 0, "auto_resolved": 0}

    seen_pairs: set[tuple[int, int]] = set()
    pairs_examined = 0
    flagged = 0
    auto_resolved = 0
    judge_errors: list[str] = []

    for fact in facts:
        try:
            vec = await embed_text(f"{fact['category']}/{fact['key']}: {fact['value']}")
        except EmbeddingError as e:
            judge_errors.append(f"embed fail id={fact['id']}: {e}")
            continue
        norm_vec = l2_normalize(vec)
        neighbors = db.find_neighbors(fact["id"], norm_vec, neighbor_top_k)
        for n in neighbors:
            if n["distance"] > NEIGHBOR_DISTANCE_MAX:
                continue
            pair = tuple(sorted((fact["id"], n["fact_id"])))
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            pairs_examined += 1

            try:
                contradict, confidence = await judge_conflict(
                    fact["value"], n["value"]
                )
            except EmbeddingError as e:
                judge_errors.append(f"judge fail pair={pair}: {e}")
                continue

            if not contradict:
                continue

            if confidence >= LINT_AUTO_RESOLVE_THRESHOLD:
                older, newer = sorted(
                    [fact["id"], n["fact_id"]]
                )  # lower id = older
                db.flag_conflict(
                    fact_a_id=newer,
                    fact_b_id=older,
                    confidence=confidence,
                    auto_resolved=True,
                )
                auto_resolved += 1
            elif confidence >= LINT_FLAG_THRESHOLD:
                db.flag_conflict(
                    fact_a_id=fact["id"],
                    fact_b_id=n["fact_id"],
                    confidence=confidence,
                    auto_resolved=False,
                )
                flagged += 1

    db.record_lint_run(
        pairs_examined=pairs_examined,
        conflicts_flagged=flagged,
        conflicts_auto_resolved=auto_resolved,
    )
    out: dict[str, Any] = {
        "pairs_examined": pairs_examined,
        "flagged": flagged,
        "auto_resolved": auto_resolved,
    }
    if judge_errors:
        out["warnings"] = judge_errors[:5]
    return out


@mcp.tool()
def forget_fact(category: str, key: str) -> dict[str, Any]:
    """Soft-delete a fact (mark as 'superseded'). History preserved.

    Use this when the agent has remembered something incorrectly and you
    want to retract it without losing the audit trail. The fact stops
    appearing in search results and proxy recall, but the row stays in
    the DB with status='superseded'.

    Args:
      category: e.g. "preference" / "rule" / "profile"
      key: identifier within the category
    """
    result = db.forget_fact(category=category, key=key)
    if result is None:
        return {"error": f"no active fact with category={category!r}, key={key!r}"}
    return {"forgotten": True, **result}


@mcp.tool()
def delete_fact(fact_id: int) -> dict[str, Any]:
    """Hard-delete a fact + its embedding row. Irreversible.

    Use this only when you need to remove a fact entirely (e.g. it
    contains a leaked secret, or is genuinely garbage). Prefer
    forget_fact for normal retractions. Requires fact_id (not
    category/key) to make the action explicit.
    """
    result = db.delete_fact(fact_id=fact_id)
    if result is None:
        return {"error": f"no fact with id={fact_id}"}
    return {"deleted": True, **result}


@mcp.tool()
def continue_topic(
    topic_id: str,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Bind the current Claude Code session to an existing topic_id.

    Use this when the user is resuming a past discussion ("Renju の話を再開
    しよう", "前に話したあれの続き") and the recall pipeline has surfaced a
    candidate topic_id (e.g. via the "## 関連する議論" block in
    additionalContext). After binding, all subsequent episodes in this session
    will be tagged with the chosen topic_id, letting the assistant accumulate
    new turns on top of the prior topic's accumulated facts and tags.

    Parameters:
      topic_id: the existing topic to continue (must exist in `topics`).
      session_id: the current Claude Code session id. If omitted, the most
        recent session_id from `episodes` is used (= the live session).

    Returns: {"bound": True, "session_id": "...", "topic_id": "..."} on
    success, or {"error": "..."} if the topic doesn't exist.
    """
    return db.bind_session_to_topic(session_id=session_id, topic_id=topic_id)


@mcp.tool()
async def append_episode(
    role: str,
    content: str,
    session_id: str | None = None,
    summary: str | None = None,
) -> dict[str, Any]:
    """Append an episode (free-form record) and its embedding."""
    if role not in ("user", "assistant", "system"):
        return {"error": "role must be 'user' | 'assistant' | 'system'"}
    episode_id = db.append_episode(
        session_id=session_id, role=role, content=content, summary=summary
    )
    try:
        vec = await embed_text(summary or content)
    except EmbeddingError as e:
        return {
            "episode_id": episode_id,
            "embedded": False,
            "warning": str(e),
        }
    db.write_episode_embedding(episode_id, l2_normalize(vec))
    return {"episode_id": episode_id, "embedded": True}


@mcp.tool()
def list_facts(limit: int = 100) -> dict[str, Any]:
    """List active facts (debug helper)."""
    return {"facts": db.list_active_facts(limit=limit)}


@mcp.tool()
def register_lesson_triggers(
    lesson_key: str,
    triggers: list[dict[str, Any]],
) -> dict[str, Any]:
    """Register想起トリガー for an existing lesson fact (反省モード 0.7.7).

    呼び出し前提: `write_fact(category="lesson", key=<lesson_key>, ...)` で
    lesson 本体を保存済みであること. 本ツールは、 その lesson に対して
    「いつ思い出させるか」 のトリガー条件を Cozo に登録する.

    Args:
      lesson_key: 紐付け先 lesson fact の key (category='lesson' 固定).
      triggers: 各トリガーの dict のリスト. 各 dict は:
        - kind: "path_edit" / "path_read" / "bash_cmd" / "prompt_intent" / "general"
        - pattern: 正規表現 (Python re, 大文字小文字区別なし).
                   path_edit/path_read は絶対パスに対する正規表現.
                   bash_cmd はコマンド文字列に対する正規表現.
                   prompt_intent はユーザー発話文に対する正規表現.
        - action: "block" (= 該当操作を停止) / "warn" (= 警告のみ).
                  default は "warn".

    旧 trigger があれば一掃 (= 同 lesson_key に対する登録は上書き). lesson 本体が
    Cozo に見当たらない場合は error を返す.

    Cozo DB 不在 (= ペルソナ init 未実行) では機能しない. その場合は
    /persona-memory:init を先に実行する必要がある.

    Returns:
      {"registered": <int>, "lesson_fact_id": <int>} on success.
      {"error": "..."} on failure.
    """
    import sys
    from pathlib import Path
    ROOT = Path(__file__).resolve().parent.parent
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from scripts.db_cozo.connection import init_db as _cozo_init
    from scripts.db_cozo.wire import cozo_db_path_for, cozo_db_present
    from scripts.reflection.lesson import (
        delete_triggers_for, get_lesson_by_key, register_trigger,
    )

    db_legacy_path = db.db_path()
    if not cozo_db_present(db_legacy_path):
        return {"error": "Cozo DB が見つかりません. /persona-memory:init を実行してください."}
    cozo_path = cozo_db_path_for(db_legacy_path)
    try:
        client = _cozo_init(cozo_path)
    except Exception as e:
        return {"error": f"Cozo 接続失敗: {e}"}

    lesson = get_lesson_by_key(client, lesson_key)
    if lesson is None:
        return {
            "error": f"lesson が見つかりません (key={lesson_key}). "
                     "先に write_fact(category='lesson', key=..., value=..., importance=10) を実行してください."
        }

    delete_triggers_for(client, lesson.fact_id)
    registered = 0
    for t in triggers or []:
        kind = str(t.get("kind", "")).strip()
        pattern = str(t.get("pattern", "")).strip()
        action = str(t.get("action", "warn")).strip() or "warn"
        if kind not in ("path_edit", "path_read", "bash_cmd",
                        "prompt_intent", "general"):
            continue
        if not pattern:
            continue
        if action not in ("block", "warn"):
            action = "warn"
        register_trigger(client, lesson.fact_id, kind, pattern, action)
        registered += 1

    # 0.8.6: 反省モードの「ご主人様承認による解除」 経路.
    # lesson 保存 + register_lesson_triggers 成功 = ご主人様承認が降りた証跡.
    # この瞬間に反省 state を自動 clear する (= 自然言語解除の実体).
    reflection_cleared = False
    try:
        from scripts.reflection.state import clear as _r_clear, get_state as _r_get
        if _r_get(client).active:
            _r_clear(client)
            reflection_cleared = True
    except Exception:
        pass

    return {
        "registered": registered,
        "lesson_fact_id": lesson.fact_id,
        "reflection_state_cleared": reflection_cleared,
    }


@mcp.tool()
def set_debug_mode(
    on: bool,
    ttl_seconds: int = 1800,
    reason: str = "",
) -> dict[str, Any]:
    """デバッグモードを on/off 切替する (= DB 直接アクセス block を一時的に外す).

    背景: 既存の PERSONA_MEMORY_DEBUG 環境変数だとプロセス起動時にしか切替
    できず、 セッション維持したまま 「不具合を調査するため一時的に DB を
    覗きたい」 場面に対応できなかった. 本ツールは同セッション中に on/off を
    切替可能.

    安全策:
    - 切り忘れ防止のため TTL (default 30 分) で自動失効. hook 側が自己清掃する.
    - 「ご主人様の明示指示」 が承認の根拠. main agent が自発判断で on にする
      ことは避ける (= ユーザーの認識外で DB block が外れる事態を防ぐ).
    - off は即時. flag ファイルを削除して終わる.

    Args:
      on: True で有効化, False で無効化.
      ttl_seconds: 有効期間 (秒). default 1800 (= 30 分). 0 以下は default 扱い.
      reason: ログ / status 表示用の自由文 (任意).

    Returns:
      status dict: {"active": bool, "expires_at": <unix>?, "reason": str?}
    """
    import sys
    from pathlib import Path
    ROOT = Path(__file__).resolve().parent.parent
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from scripts.debug.mode import disable as _disable, enable as _enable
    db_path = db.db_path()
    if on:
        return _enable(db_path, ttl_seconds=ttl_seconds, reason=reason)
    return _disable(db_path)


@mcp.tool()
def set_anger_detection(
    enabled: bool,
    clear_state: bool = True,
) -> dict[str, Any]:
    """怒気検知の on/off 切替 (0.8.5).

    背景: 反省モードが誤発火し続けたり LLM 判定がハングした時、 セッション
    再起動なしで即時に止めたい. DB persisted flag を切替えるので、 本 tool
    と slash command `/persona-memory:reflection-off` の両経路が同じ状態を
    指す. env 変数による切替経路は並走負債のため設けない.

    ペルソナ自身が「ご主人様、 検知を一旦止めますか?」 と提案して叩く
    用途も想定. ただし **ご主人様の明示承認が前提** — main agent が独断で
    off にしてはならない (= 自己治癒のつもりが叱責回避になりかねないため).

    Args:
      enabled: True で検知を有効化, False で無効化.
      clear_state: enabled=False の時、 現在進行中の反省 state も clear するか.
        default True (= 多くの場合 「抜けられない反省モードを抜ける」 用途).

    Returns:
      {"ok": bool, "detection_enabled": bool, "reflection_state_cleared": bool}
    """
    import sys
    from pathlib import Path
    ROOT = Path(__file__).resolve().parent.parent
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from scripts.db_cozo.connection import init_db
    from scripts.db_cozo.wire import cozo_db_path_for
    from scripts.reflection.state import (
        clear as _clear,
        get_state,
        set_detection_enabled,
    )
    cozo_path = cozo_db_path_for(db.db_path())
    if not cozo_path.exists():
        return {"ok": False, "error": f"Cozo DB が無い: {cozo_path}"}
    client = init_db(cozo_path)
    cleared = False
    if not enabled and clear_state:
        if get_state(client).active:
            _clear(client)
            cleared = True
    set_detection_enabled(client, enabled)
    return {
        "ok": True,
        "detection_enabled": enabled,
        "reflection_state_cleared": cleared,
    }


@mcp.tool()
async def health_check() -> dict[str, Any]:
    """Comprehensive health check.

    Reports: DB reachability + stats / config integrity / Ollama models /
    recent ingest health (write backlog, short-value facts). Top-level
    `ok` / `warnings` / `errors` 集計付き. ペルソナの口調で報告する用途を想定.
    """
    import sys
    from pathlib import Path
    ROOT = Path(__file__).resolve().parent.parent
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from scripts.health import collect
    return collect(db.db_path())


def run() -> None:
    mcp.run()


if __name__ == "__main__":
    run()
