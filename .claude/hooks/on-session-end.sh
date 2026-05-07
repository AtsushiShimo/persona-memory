#!/bin/sh
# SessionEnd hook — MVP では no-op.
#
# spec §11 では raw_dump + GC (sync) と大量 lint パス (detach) の予定。
# どれも MVP 範囲外:
# - raw 保存はターン単位で走り済み
# - GC は北極星 (記憶を消さない) と相反するので慎重に設計が必要 (post-MVP)
# - lint LLM は post-MVP
#
# 旧版コード (persist_before_compact.py 等) は新スキーマと互換でないため呼ばない。

[ -n "$PERSONA_SUMMARY_CHILD" ] && exit 0
[ -n "$PERSONA_ESCALATION_CHILD" ] && exit 0
exit 0
