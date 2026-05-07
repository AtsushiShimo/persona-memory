#!/bin/sh
# PreCompact hook — MVP では no-op (raw 保存は UserPromptSubmit / Stop で完結).
#
# spec §11 では raw_dump (sync) + Claude 経由 session summary (detach) の予定。
# raw 保存はターン単位ですでに走っているので「コンパクト直前にもう一度逃す」 動作は
# 必須ではない。session summary も MVP には含まれない (post-MVP).
#
# 旧版コード (persist_before_compact.py / claude_session_summary.py) は新スキーマと
# 互換でないため呼ばない。

[ -n "$PERSONA_SUMMARY_CHILD" ] && exit 0
[ -n "$PERSONA_ESCALATION_CHILD" ] && exit 0
exit 0
