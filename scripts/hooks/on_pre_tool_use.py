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

DENY_MESSAGE = (
    "persona-memory の DB への直接アクセスは禁止されています。"
    "記憶の参照は UserPromptSubmit hook の additionalContext (= recall LLM の結果) "
    "経由でのみ行います。recall で出てこなかった情報は「その記憶は今の検索では"
    "出てこなかった」 と正直に答えるか、ユーザーに別の言い方で聞き直すよう依頼"
    "してください。直接 SQL は recall パイプラインの意図解釈・関連度評価・"
    "supersedes チェーン解決をすべてスキップするため、記憶の精度を破壊します。"
)


def _is_blocked_bash(command: str) -> bool:
    """subcommand 単位で「先頭が危険ツール + .persona-memory/*.db を引数に持つ」 を判定。

    なぜ subcommand に分解するか: `git commit -m "..sqlite3.."` のように引数の
    中に文字列として現れただけでブロックしないため。
    """
    if not command:
        return False
    for sub in _CMD_SPLIT_RE.split(command):
        if _DANGEROUS_CMD_RE.match(sub) or _FILE_READ_CMD_RE.match(sub):
            return True
    return False


def _is_blocked_read(file_path: str) -> bool:
    if not file_path:
        return False
    return bool(_DB_READ_PATH_RE.search(file_path))


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}

    blocked = False
    if tool_name == "Bash":
        blocked = _is_blocked_bash(tool_input.get("command", "") or "")
    elif tool_name == "Read":
        blocked = _is_blocked_read(tool_input.get("file_path", "") or "")

    if blocked:
        output = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": DENY_MESSAGE,
            }
        }
        print(json.dumps(output, ensure_ascii=False))

    return 0


if __name__ == "__main__":
    sys.exit(main())
