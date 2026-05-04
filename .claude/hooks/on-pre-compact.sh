#!/bin/sh
# PreCompact hook for persona-memory.
# 1) raw_dump 保存 (sync, 必須)
# 2) Claude による高品質 session summary を背景で生成 (detach, optional)

# 子セッション (claude -p で生成された summary 用) ではこの hook を skip
[ -n "$PERSONA_SUMMARY_CHILD" ] && exit 0

SCRIPT_HOME="${CLAUDE_PLUGIN_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
export SCRIPT_HOME
RAW_SCRIPT="$SCRIPT_HOME/scripts/persist_before_compact.py"
SUMMARY_SCRIPT="$SCRIPT_HOME/scripts/claude_session_summary.py"

[ -f "$RAW_SCRIPT" ] || exit 0

# shellcheck disable=SC1091
. "$SCRIPT_HOME/scripts/load_persona_env.sh"

[ -x "${PERSONA_PYTHON:-}" ] || exit 0

# stdin (hook payload) を temp file に退避 (sync + bg 両方で使う)
INPUT_TMP=$(mktemp -t persona_compact_input.XXXXXX)
cat > "$INPUT_TMP"

# 1) raw_dump を sync で保存
"$PERSONA_PYTHON" "$RAW_SCRIPT" < "$INPUT_TMP"

# 2) Claude session summary を完全 detach で起動 (claude -p は数十秒かかる)
if [ -f "$SUMMARY_SCRIPT" ]; then
  (
    setsid "$PERSONA_PYTHON" "$SUMMARY_SCRIPT" < "$INPUT_TMP" >/dev/null 2>&1
    rm -f "$INPUT_TMP"
  ) </dev/null >/dev/null 2>&1 &
else
  rm -f "$INPUT_TMP"
fi

exit 0
