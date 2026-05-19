"""write LLM 主エントリ (0.8.0 — Cozo 単独経路).

detached child として起動され、 新規 episode を fact + 議論ノードに変換する.

フロー:
1. stdin から JSON 受信: {"episode_ids": [42, 43], "buffer_n": 10}
2. 各 episode について:
   a. 直前 BUFFER_N 発話を episode から取得 (同一 session に絞る)
   b. write LLM で fact candidates + 議論ノード候補を抽出
   c. 各 candidate を embedding → Cozo find_match → apply_candidate
   d. 議論ノード/エッジを Cozo に保存 (graph_extract 経由)
   e. episode に embedding 付与
3. 触れた fact_id を lint spawn に渡す (tail で近傍矛盾検査)
"""
from __future__ import annotations

import json
import os
import sys
from typing import Iterable

from scripts.db_cozo.connection import init_db
from scripts.db_cozo.fact_persist import apply_candidate, find_match
from scripts.db_cozo.repo import fetch_buffer, fetch_episode
from scripts.db_cozo.wire import (
    cozo_db_path_for, cozo_db_present, maybe_cozo_extract_graph,
)
from scripts.escalate.claude_p import invoke_claude
from scripts.escalate.decide import should_escalate
from scripts.shared.env import get_db_path
from scripts.shared.ollama import LLMClient, OllamaClient
from scripts.write.extract import (
    FactCandidate, WRITE_MODEL, build_prompt, extract_facts_and_nodes,
    parse_response,
)

EMBED_MODEL = os.environ.get("PERSONA_EMBED_MODEL", "nomic-embed-text")
DEFAULT_BUFFER_N = int(
    os.environ.get("PERSONA_WRITE_BUFFER_N",
                   os.environ.get("PERSONA_BUFFER_N", "10"))
)


def _extract_via_claude(role: str, content: str, buffer: list[dict]) -> list[FactCandidate]:
    """長文時は Claude に抽出を任せる (escalation)."""
    prompt = build_prompt(role, content, buffer)
    r = invoke_claude(prompt)
    if not r.success:
        return []
    return parse_response(r.text)


def _persist_episode_embedding(
    client, episode_id: int, vec: list[float],
) -> None:
    """episode の embedding 列を更新 (= 旧 episode_embeddings table 廃止)."""
    if not vec:
        return
    row = client.run(
        "?[role, content, summary, session_id, topic_id, ts] := "
        "*episode{id: $id, role, content, summary, session_id, topic_id, "
        "timestamp: ts} :limit 1",
        {"id": episode_id},
    ).get("rows", [])
    if not row:
        return
    r = row[0]
    client.run(
        "?[id, role, content, summary, session_id, topic_id, timestamp, embedding] <- "
        "[[$id, $r, $c, $s, $sid, $tid, $ts, $emb]] "
        ":put episode {id => role, content, summary, session_id, "
        "topic_id, timestamp, embedding}",
        {"id": episode_id, "r": r[0], "c": r[1], "s": r[2],
         "sid": r[3], "tid": r[4], "ts": r[5], "emb": vec},
    )


def process_episode(
    client,
    episode_id: int,
    buffer_n: int,
    llm: LLMClient,
    write_model: str = WRITE_MODEL,
    embed_model: str = EMBED_MODEL,
) -> list[tuple[FactCandidate, str]]:
    """1 episode を処理し (candidate, action) のリストを返す."""
    episode = fetch_episode(client, episode_id)
    if not episode:
        return []
    buffer = fetch_buffer(
        client, episode_id, buffer_n,
        session_id=episode.get("session_id"),
    )

    # episode 全文 embedding
    episode_emb = None
    if episode["content"] and episode["content"].strip():
        try:
            episode_emb = llm.embed(embed_model, episode["content"])
        except Exception as e:
            sys.stderr.write(
                f"[persona-memory] episode embedding failed (id={episode_id}): {e}\n",
            )

    # long_input escalation + write LLM 抽出
    full_input = episode["content"] + "\n" + "\n".join(
        b.get("content", "") for b in buffer
    )
    reason = should_escalate(input_text=full_input)
    node_candidates: list = []
    if reason == "long_input":
        candidates = _extract_via_claude(
            episode["role"], episode["content"], buffer,
        )
    else:
        candidates, node_candidates = extract_facts_and_nodes(
            role=episode["role"],
            content=episode["content"],
            buffer=buffer,
            client=llm,
            model=write_model,
        )

    # seen_keys safety net
    seen_keys: dict[tuple[str, str], int] = {}
    for cand in candidates:
        kid = (cand.category, cand.key)
        cnt = seen_keys.get(kid, 0)
        seen_keys[kid] = cnt + 1
        if cnt > 0:
            new_key = f"{cand.key}_{cnt + 1}"
            sys.stderr.write(
                f"[persona-memory] write LLM duplicate (cat={cand.category}, "
                f"key={cand.key}) in batch — renamed to '{new_key}'\n",
            )
            cand.key = new_key

    # candidate embeddings (= <cat>/<key>: <value>)
    cand_embeds: list[list[float]] = []
    for cand in candidates:
        text = f"{cand.category}/{cand.key}: {cand.value}"
        try:
            cand_embeds.append(llm.embed(embed_model, text))
        except Exception:
            cand_embeds.append([])

    # episode embedding 書き込み
    if episode_emb is not None:
        try:
            _persist_episode_embedding(client, episode_id, episode_emb)
        except Exception as e:
            sys.stderr.write(
                f"[persona-memory] episode embed persist failed (id={episode_id}): {e}\n",
            )

    # 0.7.6 Cozo リアルタイム議論グラフ (= wire.maybe_cozo_extract_graph)
    try:
        db_path = get_db_path()
        if db_path is not None:
            maybe_cozo_extract_graph(
                db_path,
                role=episode["role"],
                content=episode["content"],
                session_id=episode["session_id"],
                buffer=buffer,
                episode_id=episode_id,
            )
    except Exception as e:
        sys.stderr.write(
            f"[persona-memory] cozo extract_graph failed: {e}\n",
        )

    if not candidates:
        return []

    # fact apply (Cozo)
    results: list[tuple[FactCandidate, str]] = []
    for cand, emb in zip(candidates, cand_embeds):
        try:
            match = find_match(client, cand.category, cand.key, emb)
            action = apply_candidate(
                client, cand, match, emb,
                source="conversation", reason=cand.reason,
            )
            results.append((cand, action))
        except Exception as e:
            sys.stderr.write(
                f"[persona-memory] cozo apply_candidate failed: {e}\n",
            )
    return results


def run(
    episode_ids: Iterable[int],
    buffer_n: int = DEFAULT_BUFFER_N,
    llm: LLMClient | None = None,
) -> int:
    db_path = get_db_path()
    if db_path is None:
        return 0
    if not cozo_db_present(db_path):
        sys.stderr.write(
            "[persona-memory] Cozo DB が見つかりません (write). "
            "/persona-memory:upgrade を実行してください.\n",
        )
        return 0
    cozo_path = cozo_db_path_for(db_path)
    cli = llm or OllamaClient()
    touched_fact_ids: list[int] = []

    for eid in episode_ids:
        try:
            client = init_db(cozo_path)
            results = process_episode(client, eid, buffer_n, cli)
            for cand, action in results:
                if action not in ("insert", "supersede"):
                    continue
                # 触れた fact の id を Cozo から再取得
                row = client.run(
                    "?[id] := *fact{id, category, key, status: 'active'}, "
                    "category = $c, key = $k :limit 1",
                    {"c": cand.category, "k": cand.key},
                ).get("rows", [])
                if row:
                    touched_fact_ids.append(row[0][0])
        except Exception as e:
            sys.stderr.write(
                f"[persona-memory] write process_episode {eid} failed: {e}\n",
            )

    if touched_fact_ids:
        try:
            from scripts.hooks.spawn import spawn_lint
            spawn_lint(touched_fact_ids, trigger="write_tail")
        except Exception as e:
            sys.stderr.write(f"[persona-memory] spawn_lint failed: {e}\n")
    return 0


STDIN_WAIT_TIMEOUT = float(os.environ.get("PERSONA_WRITE_STDIN_TIMEOUT", "30"))


def _read_stdin_with_timeout(timeout: float) -> str | None:
    """stdin から payload を読む. timeout 秒以内に何も来なければ None を返す.

    対策の意図: spawn 側 (hook) が `Popen` 直後にハーネスごと死ぬと、 子は
    `json.load(sys.stdin)` で EOF 来ない stdin を永遠に待ち続け、 zombie 化する.
    `start_new_session=True` で切り離されているので親死亡では巻き込まれない.
    select() で stdin に読める/EOF があるかをまず確認し、 アイドルなら諦める.
    """
    import select
    r, _, _ = select.select([sys.stdin], [], [], timeout)
    if not r:
        return None
    return sys.stdin.read()


def main() -> int:
    raw = _read_stdin_with_timeout(STDIN_WAIT_TIMEOUT)
    if raw is None:
        # parent が payload を流す前に死んだ等. 静かに終了 (= zombie 化させない).
        return 0
    try:
        payload = json.loads(raw)
    except Exception:
        return 0
    episode_ids = payload.get("episode_ids") or []
    if not episode_ids:
        return 0
    buffer_n = int(payload.get("buffer_n", DEFAULT_BUFFER_N))
    return run(episode_ids, buffer_n)


if __name__ == "__main__":
    sys.exit(main())
