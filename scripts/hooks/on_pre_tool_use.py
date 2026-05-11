"""PreToolUse hook — agent が persona-memory DB に直接アクセスするのをブロック.

意図: recall LLM 経由 (UserPromptSubmit の additionalContext) でのみ記憶を
参照させる設計。直接 SQL や cat で DB を覗くと、recall の意図解釈・関連度
評価・supersedes 解決をすべてスキップしてしまい、改善が効かなくなる。

ブロック対象:
- Bash ツール経由の sqlite3 / cat / xxd / hexdump で .persona-memory/ 配下を触る
- Read ツールで .persona-memory/<name>.db を開く

ブロック方法: stdout に permissionDecision=deny の JSON を出して exit 0。
"""
from __future__ import annotations

import json
import os
import re
import sys

# DB アクセスっぽい Bash command を検出する。
# false positive (例: コミットメッセージ内の "sqlite3" "...persona-memory..." 文字列)
# を避けるため、subcommand の **先頭が** 危険ツール本体である場合のみブロックする。
_DANGEROUS_CMD_RE = re.compile(
    r"^\s*"
    r"(?:[A-Z_][A-Z0-9_]*=\S+\s+)*"           # optional env-var prefix (FOO=bar cmd ...)
    r"(?:[\w./-]*/)?"                          # optional path prefix (/usr/bin/sqlite3)
    r"(?:sqlite3|xxd|hexdump|strings)\b"      # 危険ツール本体
    r"[^&|;]*"                                  # 残りの引数 (operator までで止まる)
    r"\.persona-memory[/\\][^/\\\s]*\.db\b",   # .persona-memory/<name>.db を引数に含む
    re.IGNORECASE,
)

# cat / less / head / tail などのファイル read 系は別パターン
# (引数として直接 .persona-memory/*.db を渡しているケースのみ)
_FILE_READ_CMD_RE = re.compile(
    r"^\s*"
    r"(?:[A-Z_][A-Z0-9_]*=\S+\s+)*"
    r"(?:[\w./-]*/)?"
    r"(?:cat|head|tail|less|more|od|bat)\b"
    r"\s+[^&|;]*?"
    r"\.persona-memory[/\\][^/\\\s]*\.db\b",
    re.IGNORECASE,
)

# subcommand に分解 (&&, ||, ;, | で区切る)
_CMD_SPLIT_RE = re.compile(r"(?:&&|\|\||;|\|)")

_DB_READ_PATH_RE = re.compile(r"\.persona-memory[/\\][^/\\]*\.db$", re.IGNORECASE)

# Claude Code 標準 auto-memory のパス検出
# 例: /Users/x/.claude/projects/-Users-x-proj/memory/foo.md
# 例: ~/.claude/projects/<key>/memory/MEMORY.md
_CC_AUTO_MEMORY_RE = re.compile(
    r"\.claude/projects/[^/]+/memory(?:/|$)",
    re.IGNORECASE,
)

# Claude Code auto-memory を Bash で書こうとするケース
# (Write/Edit ツールを通さない workaround を防ぐ)
# false positive 防止: subcommand の **先頭が** 該当コマンドの場合のみブロック。
# コミットメッセージや echo 引数に文字列として現れただけでは止めない。
_AUTO_MEMORY_BASH_CMD_RE = re.compile(
    r"^\s*"
    r"(?:[A-Z_][A-Z0-9_]*=\S+\s+)*"
    r"(?:[\w./-]*/)?"
    r"(?:tee|mkdir|touch|cp|mv|ln)\b"
    r"[^&|;]*?"
    r"\.claude/projects/[^/]+/memory(?:[/\\]|\b)",
    re.IGNORECASE,
)

# `> file` / `>> file` の redirect target が auto-memory パスならブロック。
# `>` の **直後の token** が auto-memory パスである場合のみ反応 (途中の引数等
# に同じ文字列が現れただけでは止めない)。
_AUTO_MEMORY_REDIRECT_RE = re.compile(
    r"(?:^|\s)>{1,2}\s*\S*\.claude/projects/[^/]+/memory(?:[/\\]|\b)",
    re.IGNORECASE,
)

# Write/Edit 系のツール一覧
_WRITE_TOOL_NAMES = {"Write", "Edit", "MultiEdit", "NotebookEdit"}

DENY_MESSAGE_DB = (
    "persona-memory の DB への直接アクセスは禁止されています。"
    "記憶の参照は UserPromptSubmit hook の additionalContext (= recall LLM の結果) "
    "経由でのみ行います。recall で出てこなかった情報は「その記憶は今の検索では"
    "出てこなかった」 と正直に答えるか、ユーザーに別の言い方で聞き直すよう依頼"
    "してください。直接 SQL は recall パイプラインの意図解釈・関連度評価・"
    "supersedes チェーン解決をすべてスキップするため、記憶の精度を破壊します。"
)

DENY_MESSAGE_AUTO_MEMORY = (
    "Claude Code 標準の auto-memory (~/.claude/projects/<project>/memory/ 配下) "
    "への書き込み・読み込みは禁止されています。このプロジェクトでは persona-memory "
    "プラグインの DB に記憶を集約する方針のため、auto-memory への蓄積は記憶を"
    "分散・断片化させます。記憶したい内容は会話の中でユーザーに自然に話せば、"
    "プラグイン側が自動で persona-memory DB に蓄積します。"
)


def _is_blocked_bash(command: str) -> str | None:
    """subcommand 単位で危険コマンドを判定。戻り値: deny reason or None.

    なぜ subcommand に分解するか: `git commit -m "..sqlite3.."` のように引数の
    中に文字列として現れただけでブロックしないため。
    """
    if not command:
        return None
    for sub in _CMD_SPLIT_RE.split(command):
        if _DANGEROUS_CMD_RE.match(sub) or _FILE_READ_CMD_RE.match(sub):
            return DENY_MESSAGE_DB
        if _AUTO_MEMORY_BASH_CMD_RE.match(sub) or _AUTO_MEMORY_REDIRECT_RE.search(sub):
            return DENY_MESSAGE_AUTO_MEMORY
    return None


def _is_blocked_read(file_path: str) -> str | None:
    if not file_path:
        return None
    if _DB_READ_PATH_RE.search(file_path):
        return DENY_MESSAGE_DB
    if _CC_AUTO_MEMORY_RE.search(file_path):
        return DENY_MESSAGE_AUTO_MEMORY
    return None


def _is_blocked_write(file_path: str) -> str | None:
    """Write / Edit / MultiEdit が auto-memory に書き込もうとしてないかチェック."""
    if not file_path:
        return None
    if _CC_AUTO_MEMORY_RE.search(file_path):
        return DENY_MESSAGE_AUTO_MEMORY
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}

    # debug mode (PERSONA_MEMORY_DEBUG set & non-empty) では DB 直接アクセス
    # block を skip する。 plugin 開発時に別 project の DB を seed / 検査する
    # 等の正当な作業を hook が誤発火で止めるのを防ぐため。
    # auto-memory block (forbid_auto_memory ルール) は debug 中も維持する —
    # こちらは「記憶を分散させない」 という保護で、 DB 直接アクセスとは別軸。
    debug_mode = bool(os.environ.get("PERSONA_MEMORY_DEBUG", "").strip())

    reason: str | None = None
    if tool_name == "Bash":
        reason = _is_blocked_bash(tool_input.get("command", "") or "")
    elif tool_name == "Read":
        reason = _is_blocked_read(tool_input.get("file_path", "") or "")
    elif tool_name in _WRITE_TOOL_NAMES:
        reason = _is_blocked_write(tool_input.get("file_path", "") or "")

    if debug_mode and reason == DENY_MESSAGE_DB:
        reason = None

    if reason:
        output = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }
        print(json.dumps(output, ensure_ascii=False))

    return 0


if __name__ == "__main__":
    sys.exit(main())
