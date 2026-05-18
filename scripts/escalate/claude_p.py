"""Claude Code 親経由で `claude -p` を子プロセスとして起動 (§14)."""
from __future__ import annotations

import os
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


def log_escalation(*_args, **_kwargs) -> None:
    """0.8.0 で SQLite 経路廃止に伴い no-op 化.

    旧 escalation_log table は SQLite 専用で、 Cozo 移行先は未設計のため
    現状ログ非保存. 必要なら後続で Cozo に escalation_log relation を新設.
    """
    return None
