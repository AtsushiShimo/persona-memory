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
    pack_embedding,
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
    db.write_fact_embedding(fact_id, pack_embedding(vec))
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
    db.write_fact_embedding(fact_id, pack_embedding(vec))
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
    blob = pack_embedding(vec)
    out: dict[str, Any] = {
        "facts": db.search_facts(
            embedding_blob=blob, top_k=top_k, category=category
        ),
    }
    if include_episodes:
        out["episodes"] = db.search_episodes(embedding_blob=blob, top_k=top_k)
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
        blob = pack_embedding(vec)
        neighbors = db.find_neighbors(fact["id"], blob, neighbor_top_k)
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
    db.write_episode_embedding(episode_id, pack_embedding(vec))
    return {"episode_id": episode_id, "embedded": True}


@mcp.tool()
def list_facts(limit: int = 100) -> dict[str, Any]:
    """List active facts (debug helper)."""
    return {"facts": db.list_active_facts(limit=limit)}


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
