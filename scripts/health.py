"""persona-memory の包括的 health check (0.8.0 — Cozo 単独).

MCP tool (`server.main.health_check`) と `/persona-memory:health` slash
command 両方から呼ばれる. 返り値は dict[str, Any] で JSON 直結可能.
"""
from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path
from typing import Any

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
EMBED_MODEL = os.environ.get("PERSONA_EMBED_MODEL", "nomic-embed-text")
LIGHT_MODEL = os.environ.get(
    "PERSONA_LIGHT_MODEL", os.environ.get("PERSONA_JUDGE_MODEL", "gemma3:4b"),
)
HEAVY_MODEL = os.environ.get("PERSONA_HEAVY_MODEL", "gemma3:12b")
SCHEMA_VERSION = "cozo-1"


def _check_db(db_path: Path) -> dict[str, Any]:
    """Cozo DB の存在 + 統計."""
    from scripts.db_cozo.wire import cozo_db_path_for
    cozo_path = cozo_db_path_for(db_path)
    out: dict[str, Any] = {"path": str(cozo_path)}
    if not cozo_path.exists():
        return {**out, "ok": False, "error": "Cozo DB が存在しない"}
    try:
        from scripts.db_cozo.connection import init_db
        client = init_db(cozo_path)
        facts = client.run(
            "?[n] := *fact{id}, n = id",
        ).get("rows", [])
        episodes = client.run(
            "?[n] := *episode{id}, n = id",
        ).get("rows", [])
        active = client.run(
            "?[n] := *fact{id, status: 'active'}, n = id",
        ).get("rows", [])
        return {
            **out, "ok": True,
            "schema_version": SCHEMA_VERSION,
            "facts_active": len(active),
            "facts_total": len(facts),
            "episodes": len(episodes),
        }
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


def collect(db_path: Path) -> dict[str, Any]:
    """各検査を集約. warnings / errors はトップで集計."""
    db = _check_db(db_path)
    cfg = _check_config(db_path)
    ollama = _check_ollama()

    warnings: list[str] = []
    errors: list[str] = []

    if not db.get("ok"):
        errors.append(f"db: {db.get('error', 'unknown')}")
    if not ollama.get("reachable"):
        warnings.append(f"ollama unreachable: {ollama.get('error', '')}")
    elif not all(ollama.get("models_pulled", {}).values()):
        warnings.append("必要 Ollama モデルが揃っていない")

    return {
        "ok": len(errors) == 0,
        "warnings": warnings,
        "errors": errors,
        "db": db,
        "config": cfg,
        "ollama": ollama,
    }
