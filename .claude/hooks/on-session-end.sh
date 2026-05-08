#!/bin/sh
# SessionEnd hook — session 全体の summary を生成 + context fact として保存.
# Claude エスカレーション + embedding 生成があるため heavy。完全 detach 起動。

[ -n "$PERSONA_SUMMARY_CHILD" ] && exit 0
[ -n "$PERSONA_ESCALATION_CHILD" ] && exit 0

SCRIPT_HOME="${CLAUDE_PLUGIN_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
export SCRIPT_HOME

# shellcheck disable=SC1091
. "$SCRIPT_HOME/scripts/load_persona_env.sh"

[ -x "${PERSONA_PYTHON:-}" ] || exit 0

# stdin (hook payload) を temp file に退避
INPUT_TMP=$(mktemp -t persona_session_end_input.XXXXXX)
cat > "$INPUT_TMP"

# 完全 detach: setsid で session 切り離し、出力捨てる
(
  PYTHONPATH="$SCRIPT_HOME${PYTHONPATH:+:$PYTHONPATH}" \
    setsid "$PERSONA_PYTHON" -m scripts.hooks.on_session_end < "$INPUT_TMP" >/dev/null 2>&1
  rm -f "$INPUT_TMP"
) </dev/null >/dev/null 2>&1 &

exit 0
