"""反省モード state 管理.

state は Cozo の reflection_state relation に保存. プロセス間で共有 (= hook が
別プロセスから呼ばれても整合する) するため. ファイルベース (json) も検討したが、
Cozo に既に接続している経路 (= boot 注入や cozo_active 判定) と一元化したほうが
壊れにくい.

state schema (key/value 形式):
- "active": "1" or "0"
- "started_at": ISO8601
- "trigger_episode_id": str(int)
- "anger_phrase": str
- "turn_count": str(int)  反省モード突入以降のターン数
- "detection_enabled": "1" or "0"  怒気検知の on/off フラグ
  (0.8.5 — slash command `/persona-memory:reflection-off` や MCP tool
  `set_anger_detection` から切替). 未設定時は有効扱い (= default on).
  env 変数による切替は廃止 (並走負債を作らないため DB 一元管理).

env:
- PERSONA_REFLECTION_MAX_TURNS (default 20): 解除されないまま N ターン続いたら
  強制 auto-clear (= 無限ループ防止セーフティネット)
"""
from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass

from pycozo.client import Client

MAX_TURNS = int(os.environ.get("PERSONA_REFLECTION_MAX_TURNS", "20"))


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _put(client: Client, key: str, value: str) -> None:
    client.run(
        "?[key, value] <- [[$k, $v]] :put reflection_state {key => value}",
        {"k": key, "v": value},
    )


def _get(client: Client, key: str) -> str | None:
    res = client.run(
        "?[value] := *reflection_state{key: $k, value} :limit 1",
        {"k": key},
    )
    rows = res.get("rows", [])
    return rows[0][0] if rows else None


def _rm_all(client: Client) -> None:
    keys = ("active", "started_at", "trigger_episode_id",
            "anger_phrase", "turn_count")
    for k in keys:
        try:
            client.run(
                "?[key] <- [[$k]] :rm reflection_state {key}",
                {"k": k},
            )
        except Exception:
            pass


@dataclass
class ReflectionState:
    active: bool
    started_at: str = ""
    trigger_episode_id: int = 0
    anger_phrase: str = ""
    turn_count: int = 0


def get_state(client: Client) -> ReflectionState:
    if _get(client, "active") != "1":
        return ReflectionState(active=False)
    try:
        eid = int(_get(client, "trigger_episode_id") or "0")
    except ValueError:
        eid = 0
    try:
        tc = int(_get(client, "turn_count") or "0")
    except ValueError:
        tc = 0
    return ReflectionState(
        active=True,
        started_at=_get(client, "started_at") or "",
        trigger_episode_id=eid,
        anger_phrase=_get(client, "anger_phrase") or "",
        turn_count=tc,
    )


def enter(client: Client, episode_id: int, anger_phrase: str) -> None:
    """反省モード突入. 既に active なら turn_count を増やすだけ."""
    cur = get_state(client)
    if cur.active:
        _put(client, "turn_count", str(cur.turn_count + 1))
        return
    _put(client, "active", "1")
    _put(client, "started_at", _now())
    _put(client, "trigger_episode_id", str(episode_id))
    _put(client, "anger_phrase", anger_phrase or "")
    _put(client, "turn_count", "1")


def increment_turn(client: Client) -> int:
    cur = get_state(client)
    if not cur.active:
        return 0
    new_tc = cur.turn_count + 1
    _put(client, "turn_count", str(new_tc))
    return new_tc


def clear(client: Client) -> None:
    """反省モード解除. 警告なし."""
    _rm_all(client)


def should_force_clear(client: Client) -> bool:
    """N ターン経過していたら強制 auto-clear すべき (セーフティネット)."""
    cur = get_state(client)
    return cur.active and cur.turn_count >= MAX_TURNS


# ── 怒気検知の動的 on/off (0.8.5) ──
# 検知の on/off は DB persisted flag のみで管理する. env 変数による切替経路は
# 並走負債になるため排除. slash command / MCP tool から動的に切れる.

def is_detection_enabled(client: Client) -> bool:
    """怒気検知が有効か. 未設定 (= 新規 DB) は有効扱い."""
    v = _get(client, "detection_enabled")
    if v is None:
        return True
    return v == "1"


def set_detection_enabled(client: Client, enabled: bool) -> None:
    """怒気検知の動的 on/off を persist."""
    _put(client, "detection_enabled", "1" if enabled else "0")
