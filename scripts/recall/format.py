"""recall 結果を markdown additionalContext に整形."""
from __future__ import annotations

from scripts.recall.search import RecalledFact


def to_additional_context(facts: list[RecalledFact]) -> str:
    if not facts:
        return ""
    lines = ["## 関連する記憶"]
    for f in facts:
        lines.append(f"- [{f.category}/{f.key}] {f.value} (importance={f.importance})")
    return "\n".join(lines)
