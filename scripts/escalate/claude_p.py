"""Claude Code 親経由で `claude -p` を子プロセスとして起動 (§14)."""
from __future__ import annotations

import os
import sqlite3
import subprocess
from dataclasses import dataclass

ESCALATION_TIMEOUT = float(os.environ.get("PERSONA_ESCALATE_TIMEOUT", "120"))


@dataclass
class EscalationResult:
    text: str
    success: bool
    error: str = ""


def invoke_claude(prompt: str, timeout: float = ESCALATION_TIMEOUT) -> EscalationResult:
    """`claude -p <prompt>` を実行し標準出力を返す。

    PERSONA_ESCALATION_CHILD=1 を子に渡し、各 hook が再帰しないように防ぐ (§14.2).
    PERSONA_WRITE_DISABLE / PERSONA_RECALL_DISABLE も子で立てて、子の hook が
    余計な処理を走らせないようにする。
    """
    env = os.environ.copy()
    env["PERSONA_ESCALATION_CHILD"] = "1"
    env.setdefault("PERSONA_WRITE_DISABLE", "1")
    env.setdefault("PERSONA_RECALL_DISABLE", "1")
    try:
        r = subprocess.run(
            ["claude", "-p", prompt],
            env=env,
            capture_output=True,
            timeout=timeout,
            text=True,
        )
        if r.returncode != 0:
            return EscalationResult(text="", success=False, error=r.stderr.strip()[:200])
        return EscalationResult(text=r.stdout.strip(), success=True)
    except subprocess.TimeoutExpired:
        return EscalationResult(text="", success=False, error="timeout")
    except FileNotFoundError:
        return EscalationResult(text="", success=False, error="claude command not found")
    except Exception as e:
        return EscalationResult(text="", success=False, error=str(e)[:200])


def log_escalation(
    conn: sqlite3.Connection,
    reason: str,
    caller: str,
    input_size: int,
    outcome: str,
) -> None:
    conn.execute(
        "INSERT INTO escalation_log(reason, caller, input_size, outcome) "
        "VALUES (?, ?, ?, ?)",
        (reason, caller, input_size, outcome[:500]),
    )
    conn.commit()
