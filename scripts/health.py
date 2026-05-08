"""persona-memory の包括的 health check.

DB / config / Ollama / 直近 ingest 健全性を 1 関数でまとめる.
- MCP tool (`server.main.health_check`) と
- `/persona-memory:health` slash command の両方から呼ばれる.

返り値は dict[str, Any] で JSON にそのまま流せる. ok=True/False に加え,
warnings (動くが要注意) と errors (機能不全) のリストを持つ.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path
from typing import Any

from scripts.db.connection import SCHEMA_VERSION, connect
from scripts.db.repo import get_meta

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
EMBED_MODEL = os.environ.get("PERSONA_EMBED_MODEL", "nomic-embed-text")
LIGHT_MODEL = os.environ.get(
    "PERSONA_LIGHT_MODEL", os.environ.get("PERSONA_JUDGE_MODEL", "gemma3:4b"),
)
HEAVY_MODEL = os.environ.get("PERSONA_HEAVY_MODEL", "gemma3:12b")


def _check_db(db_path: Path) -> dict[str, Any]:
    out: dict[str, Any] = {"path": str(db_path)}
    if not db_path.exists():
        return {**out, "ok": False, "error": "DB ファイルが存在しない"}
    try:
        conn = connect(db_path)
        try:
            sv = get_meta(conn, "schema_version")
            facts_active = conn.execute(
                "SELECT COUNT(*) FROM facts WHERE status='active'",
            ).fetchone()[0]
            facts_total = conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
            episodes = conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
            fact_emb = conn.execute(
                "SELECT COUNT(*) FROM fact_embeddings",
            ).fetchone()[0]
            episode_emb = conn.execute(
                "SELECT COUNT(*) FROM episode_embeddings",
            ).fetchone()[0]
            orphan_facts = conn.execute(
                """
                SELECT COUNT(*) FROM facts f
                LEFT JOIN fact_embeddings fe ON fe.fact_id = f.id
                WHERE f.status='active' AND fe.fact_id IS NULL
                """
            ).fetchone()[0]
            stale_emb = conn.execute(
                """
                SELECT COUNT(*) FROM fact_embeddings fe
                LEFT JOIN facts f ON f.id = fe.fact_id AND f.status='active'
                WHERE f.id IS NULL
                """
            ).fetchone()[0]
            return {
                **out,
                "ok": True,
                "schema_version": sv,
                "schema_version_expected": SCHEMA_VERSION,
                "facts_active": facts_active,
                "facts_total": facts_total,
                "episodes": episodes,
                "fact_embeddings": fact_emb,
                "episode_embeddings": episode_emb,
                "orphan_active_facts": orphan_facts,
                "stale_embeddings": stale_emb,
            }
        finally:
            conn.close()
    except Exception as e:
        return {**out, "ok": False, "error": str(e)}


def _check_config(db_path: Path) -> dict[str, Any]:
    """active-persona と config.env の整合."""
    out: dict[str, Any] = {}
    persona_dir = db_path.parent
    active_file = persona_dir / "active-persona"
    if not active_file.exists():
        out["active_persona_file_present"] = False
        return out
    out["active_persona_file_present"] = True
    active = active_file.read_text(encoding="utf-8").strip()
    out["active_persona"] = active
    out["active_db_match"] = (db_path.stem == active)
    config_env = persona_dir / f"{active}.config.env"
    out["config_env_present"] = config_env.exists()
    return out


def _check_ollama(ping_models: bool = False) -> dict[str, Any]:
    """Ollama 到達 + 必要モデル + NUM_PARALLEL 設定."""
    out: dict[str, Any] = {"host": OLLAMA_HOST}
    try:
        req = urllib.request.Request(f"{OLLAMA_HOST}/api/tags")
        with urllib.request.urlopen(req, timeout=3) as r:
            data = json.loads(r.read())
        names = [m.get("name", "") for m in data.get("models", [])]
        out["reachable"] = True
        out["models_pulled"] = {
            "embed": any(n.startswith(EMBED_MODEL) for n in names),
            "light": any(n.startswith(LIGHT_MODEL) for n in names),
            "heavy": any(n.startswith(HEAVY_MODEL) for n in names),
        }
        out["models_required"] = {
            "embed": EMBED_MODEL, "light": LIGHT_MODEL, "heavy": HEAVY_MODEL,
        }
    except Exception as e:
        out["reachable"] = False
        out["error"] = str(e)
    out["num_parallel_env"] = os.environ.get("OLLAMA_NUM_PARALLEL", "(unset)")
    return out


def _check_recent_ingest(db_path: Path, sample: int = 10) -> dict[str, Any]:
    """直近 episode の write 進捗 + 直近 fact の品質 sanity."""
    out: dict[str, Any] = {}
    try:
        conn = connect(db_path)
        try:
            processed_raw = get_meta(conn, "write_processed_max_id")
            try:
                processed = int(processed_raw) if processed_raw else 0
            except ValueError:
                processed = 0
            latest = conn.execute(
                "SELECT COALESCE(MAX(id), 0) FROM episodes",
            ).fetchone()[0]
            out["last_episode_id"] = latest
            out["write_processed_max_id"] = processed
            out["episodes_pending_write"] = max(0, latest - processed)

            rows = conn.execute(
                """
                SELECT category, key, value FROM facts
                WHERE status='active' AND category NOT IN ('persona','rule')
                ORDER BY id DESC LIMIT ?
                """,
                (sample,),
            ).fetchall()
            short_value = sum(1 for _, _, v in rows if len((v or "").strip()) <= 3)
            out["recent_facts_checked"] = len(rows)
            out["recent_facts_with_short_value"] = short_value
        finally:
            conn.close()
    except Exception as e:
        out["error"] = str(e)
    return out


def collect(db_path: Path | None = None) -> dict[str, Any]:
    """全体検査をまとめて返す.

    db_path None なら PERSONA_MEMORY_DB env から取得.
    """
    if db_path is None:
        env = os.environ.get("PERSONA_MEMORY_DB", "").strip()
        db_path = Path(env) if env else None

    out: dict[str, Any] = {}
    if db_path is None:
        out["db"] = {"ok": False, "error": "PERSONA_MEMORY_DB env が未設定"}
    else:
        out["db"] = _check_db(db_path)
        out["config"] = _check_config(db_path)
        out["recent_ingest"] = _check_recent_ingest(db_path)
    out["ollama"] = _check_ollama()

    warnings: list[str] = []
    errors: list[str] = []
    db = out.get("db", {})
    if not db.get("ok"):
        errors.append(f"DB: {db.get('error', '不明な異常')}")
    sv = db.get("schema_version")
    if sv and sv != db.get("schema_version_expected"):
        warnings.append(
            f"schema_version 不一致: {sv} (期待 {db.get('schema_version_expected')})"
        )
    if db.get("orphan_active_facts", 0) > 0:
        warnings.append(
            f"active fact に embedding 無し: {db['orphan_active_facts']} 件 "
            "(/persona-memory:upgrade で復旧)"
        )
    if db.get("stale_embeddings", 0) > 0:
        warnings.append(
            f"stale embedding 残骸: {db['stale_embeddings']} 件 "
            "(/persona-memory:upgrade で削除)"
        )
    cfg = out.get("config", {})
    if cfg and not cfg.get("active_persona_file_present"):
        warnings.append("active-persona ファイルが無い (init 未実行?)")
    elif cfg.get("active_db_match") is False:
        warnings.append(
            f"active-persona ({cfg.get('active_persona')}) と DB ({db_path.stem if db_path else '?'}) "
            "が一致していない"
        )
    elif cfg.get("config_env_present") is False:
        warnings.append("config.env が見つからない")
    olm = out.get("ollama", {})
    if not olm.get("reachable"):
        errors.append(f"Ollama 到達不可: {olm.get('error', '')}")
    else:
        missing = [k for k, v in (olm.get("models_pulled") or {}).items() if not v]
        if missing:
            errors.append(
                f"Ollama に必要モデル未取得: {', '.join(missing)} "
                f"(`ollama pull` を実行)"
            )
    np_env = olm.get("num_parallel_env", "(unset)")
    if np_env != "(unset)":
        try:
            if int(np_env) >= 2:
                warnings.append(
                    f"OLLAMA_NUM_PARALLEL={np_env}: nomic-embed-text の並列処理で "
                    "embedding race が起きる事例あり (NUM_PARALLEL=1 推奨は要件次第)"
                )
        except ValueError:
            pass
    ri = out.get("recent_ingest", {})
    if ri.get("episodes_pending_write", 0) > 5:
        warnings.append(
            f"未処理 episode {ri['episodes_pending_write']} 件 "
            "(write が詰まっている / Ollama 過負荷の可能性)"
        )
    if ri.get("recent_facts_with_short_value", 0) > 3:
        warnings.append(
            "直近 fact に 3 文字以下の value が複数 — write LLM 抽出品質要確認"
        )

    out["warnings"] = warnings
    out["errors"] = errors
    out["ok"] = not errors
    return out


def main() -> int:
    """CLI entry. JSON で stdout 出力."""
    result = collect()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
