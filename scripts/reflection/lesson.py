"""lesson fact + trigger の CRUD + マッチング.

lesson は既存 fact relation に category='lesson' で保存される
(SQLite + Cozo 並走). 想起トリガーは Cozo 専用 relation lesson_trigger に持つ.

役割:
- list_active_lessons: category='lesson' な active fact の一覧
- get_triggers_for: lesson_fact_id に紐付くトリガー一覧
- register_triggers: 新規トリガー登録
- match_lessons_for_tool_call: PreToolUse hook 用. (tool_name, tool_input)
  から発火する lesson を返す
- match_lessons_for_prompt: UserPromptSubmit 用. prompt 文から prompt_intent
  系の lesson を返す
- format_lesson_block: additionalContext 用の整形
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

from pycozo.client import Client

from scripts.db_cozo.connection import next_id


def _now_jst() -> str:
    tz = dt.timezone(dt.timedelta(hours=9))
    return dt.datetime.now(tz).replace(tzinfo=None).isoformat(
        sep=" ", timespec="seconds",
    )


@dataclass
class Lesson:
    fact_id: int
    key: str
    value: str
    importance: int


@dataclass
class Trigger:
    id: int
    lesson_fact_id: int
    kind: str       # 'path_edit' / 'path_read' / 'bash_cmd' / 'prompt_intent' / 'general'
    pattern: str    # 正規表現
    action: str     # 'block' / 'warn'


@dataclass
class Match:
    lesson: Lesson
    trigger: Trigger


def list_active_lessons(client: Client) -> list[Lesson]:
    res = client.run(
        "?[id, key, value, importance] := "
        "*fact{id, category, key, value, importance, status: 'active'}, "
        "category = 'lesson'",
    )
    rows = res.get("rows", [])
    return [
        Lesson(fact_id=r[0], key=r[1], value=r[2], importance=r[3])
        for r in rows
    ]


def get_lesson_by_key(client: Client, key: str) -> Lesson | None:
    res = client.run(
        "?[id, key, value, importance] := "
        "*fact{id, category, key, value, importance, status: 'active'}, "
        "category = 'lesson', key = $k :limit 1",
        {"k": key},
    )
    rows = res.get("rows", [])
    if not rows:
        return None
    r = rows[0]
    return Lesson(fact_id=r[0], key=r[1], value=r[2], importance=r[3])


def get_triggers_for(client: Client, lesson_fact_id: int) -> list[Trigger]:
    res = client.run(
        "?[id, lesson_fact_id, kind, pattern, action] := "
        "*lesson_trigger{id, lesson_fact_id, kind, pattern, action}, "
        "lesson_fact_id = $lid",
        {"lid": lesson_fact_id},
    )
    rows = res.get("rows", [])
    return [
        Trigger(
            id=r[0], lesson_fact_id=r[1],
            kind=r[2], pattern=r[3], action=r[4],
        ) for r in rows
    ]


def list_all_triggers(client: Client) -> list[tuple[Lesson, Trigger]]:
    """active lesson に紐付く全 trigger を 1 回で取得 (hook の高頻度経路用)."""
    res = client.run(
        "?[fid, fkey, fvalue, fimp, tid, tkind, tpattern, taction] := "
        "*fact{id: fid, category: 'lesson', key: fkey, value: fvalue, "
        "importance: fimp, status: 'active'}, "
        "*lesson_trigger{id: tid, lesson_fact_id: fid, "
        "kind: tkind, pattern: tpattern, action: taction}",
    )
    out: list[tuple[Lesson, Trigger]] = []
    for r in res.get("rows", []):
        lesson = Lesson(fact_id=r[0], key=r[1], value=r[2], importance=r[3])
        trig = Trigger(
            id=r[4], lesson_fact_id=r[0],
            kind=r[5], pattern=r[6], action=r[7],
        )
        out.append((lesson, trig))
    return out


def register_trigger(
    client: Client, lesson_fact_id: int,
    kind: str, pattern: str, action: str = "warn",
) -> int:
    """新規 trigger 登録. id を返す."""
    new_id = next_id(client, "lesson_trigger")
    client.run(
        "?[id, lesson_fact_id, kind, pattern, action, created_at] <- "
        "[[$id, $lid, $k, $p, $a, $ts]] "
        ":put lesson_trigger {id => lesson_fact_id, kind, pattern, "
        "action, created_at}",
        {
            "id": new_id, "lid": lesson_fact_id, "k": kind,
            "p": pattern, "a": action, "ts": _now_jst(),
        },
    )
    return new_id


def delete_triggers_for(client: Client, lesson_fact_id: int) -> None:
    """lesson 削除 / 上書き時に紐付き trigger を一掃."""
    res = client.run(
        "?[id] := *lesson_trigger{id, lesson_fact_id: $lid}",
        {"lid": lesson_fact_id},
    )
    for r in res.get("rows", []):
        try:
            client.run(
                "?[id] <- [[$id]] :rm lesson_trigger {id}",
                {"id": r[0]},
            )
        except Exception:
            pass


# ── マッチング ─────────────────────────────────────────────────────────

_TOOL_KIND_MAP = {
    "Edit": "path_edit", "Write": "path_edit",
    "MultiEdit": "path_edit", "NotebookEdit": "path_edit",
    "Read": "path_read",
}


def _safe_search(pattern: str, target: str) -> bool:
    if not pattern or not target:
        return False
    try:
        return re.search(pattern, target, re.IGNORECASE) is not None
    except re.error:
        # 不正な正規表現 → substring fallback
        return pattern.lower() in target.lower()


def match_lessons_for_tool_call(
    client: Client, tool_name: str, tool_input: dict,
) -> list[Match]:
    """PreToolUse hook 用. 該当する lesson + trigger を全件返す."""
    out: list[Match] = []
    pairs = list_all_triggers(client)
    if not pairs:
        return out

    target_str = ""
    expected_kind: str | None = None
    if tool_name == "Bash":
        target_str = tool_input.get("command", "") or ""
        expected_kind = "bash_cmd"
    elif tool_name in _TOOL_KIND_MAP:
        target_str = tool_input.get("file_path", "") or ""
        expected_kind = _TOOL_KIND_MAP[tool_name]
    else:
        return out

    for lesson, trig in pairs:
        if trig.kind != expected_kind and trig.kind != "general":
            continue
        if _safe_search(trig.pattern, target_str):
            out.append(Match(lesson=lesson, trigger=trig))
    return out


def match_lessons_for_prompt(client: Client, prompt: str) -> list[Match]:
    """UserPromptSubmit 用. prompt_intent / general 系の lesson をマッチ."""
    out: list[Match] = []
    if not prompt:
        return out
    pairs = list_all_triggers(client)
    for lesson, trig in pairs:
        if trig.kind not in ("prompt_intent", "general"):
            continue
        if _safe_search(trig.pattern, prompt):
            out.append(Match(lesson=lesson, trigger=trig))
    return out


# ── 整形 ──────────────────────────────────────────────────────────────

def format_lesson_block_for_prompt(matches: list[Match]) -> str:
    """UserPromptSubmit の additionalContext 用ブロック."""
    if not matches:
        return ""
    lines = ["## 過去の叱責から学んだルール (該当)"]
    seen: set[int] = set()
    for m in matches:
        if m.lesson.fact_id in seen:
            continue
        seen.add(m.lesson.fact_id)
        action_label = "停止" if m.trigger.action == "block" else "警告"
        lines.append(
            f"- [{m.lesson.key}] ({action_label}) {m.lesson.value}"
        )
    return "\n".join(lines)


def format_tool_block_message(matches: list[Match], tool_name: str) -> str:
    """PreToolUse の permissionDecisionReason 用 message."""
    parts = [
        "過去の叱責から学んだルールに該当します:",
    ]
    for m in matches:
        parts.append(
            f"- [{m.lesson.key}] {m.lesson.value}"
        )
    parts.append(
        f"\n操作 ({tool_name}) を中止しました. "
        "別の方法で目的を達成できないか検討するか、 ご主人様に確認してください."
    )
    return "\n".join(parts)
