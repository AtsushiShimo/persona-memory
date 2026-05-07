"""recall 結果を markdown additionalContext に整形."""
from __future__ import annotations

from scripts.recall.search import RecalledEpisode, RecalledFact


def to_additional_context(
    facts: list[RecalledFact],
    episodes: list[RecalledEpisode] | None = None,
) -> str:
    sections: list[str] = []

    if facts:
        lines = ["## 関連する記憶"]
        for f in facts:
            lines.append(f"- [{f.category}/{f.key}] {f.value} (importance={f.importance})")
        sections.append("\n".join(lines))

    if episodes:
        lines = ["## 関連する過去の会話"]
        for e in episodes:
            lines.append(f"- [{e.timestamp}] [{e.role}] {e.content}")
        sections.append("\n".join(lines))

    return "\n\n".join(sections)
