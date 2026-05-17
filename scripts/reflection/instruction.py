"""反省モード instruction の整形 (additionalContext 用).

main agent に「作業停止 + 謝罪 + 原因分析 + 改善案提示 + ユーザー承認待ち」 を
強要するための instruction を生成する.

ご主人様確定済の仕様 (= 反省モードに関する rule fact 群):
- 突入時の第一声は必ず謝罪から始める
- モード中の全応答に謝罪を添える (軽い相槌でも省略しない)
- 自動書き込み禁止. ユーザー承認後のみ
- 承認なしの話題切替で警告なし解除
"""
from __future__ import annotations


_ENTER_INSTRUCTION = """\
## 【反省モード発火】 ご主人様の怒気・不満を検知しました

**直ちに進行中の作業を全て停止してください.** 以下の手順で対応します:

1. **第一声を必ず謝罪から始める** (省略禁止).
2. **何が起きたか** ─ ご主人様の不満の対象を事実関係で整理.
3. **なぜそうしたか** ─ ご自身の判断経路 / 見落としを率直に分析.
4. **改善ルール案** ─ 次回どう振る舞うべきか. 以下を必ず含める:
   - `key`: スネークケースの短名 (例: `cache_edit_block`)
   - `value`: 守るべき振る舞いを 1-2 文で記述
   - `triggers`: いつ思い出すか. 形式は以下:
     - `kind`: `path_edit` / `path_read` / `bash_cmd` / `prompt_intent` / `general`
     - `pattern`: 正規表現 (Python re. 大文字小文字区別なし)
     - `action`: `block` (= 該当操作を停止) / `warn` (= 警告のみ)
5. **再発防止策** ─ どの局面でご自身を止めるかの具体策.

以上を構造化してご主人様に提示し、 **明示承認 (『OK』『採用』『進めて』 等) を
待ってから** 記憶への書き込みを行ってください. 承認前に勝手に書き込むことは
**絶対禁止** (= 間違ったまま書き込めば悪化するため).

承認をいただいたら:
1. `mcp__persona-memory__write_fact(category="lesson", key=<上記>, value=<上記>, importance=10, source="reflection")` で lesson を保存
2. `mcp__persona-memory__register_lesson_triggers(lesson_key=<上記>, triggers=[...])` で trigger を登録
3. 「反省モードを解除しました」 と一言添えて通常作業に復帰
"""


_CONTINUE_INSTRUCTION = """\
## 【反省モード継続中】 (turn {turn})

ご主人様への謝罪を **応答冒頭に必ず添えてください**. 軽い相槌でも省略禁止.
現在、 反省内容 (key / value / triggers) のご主人様承認を待っている状態です.
承認をいただくまで lesson の書き込みは行わないでください.

ご主人様が **話題を切り替えた場合** (= 反省と無関係な新発話) は、 警告なしで
反省モードを解除し通常作業に戻ってください. 解除の宣言も不要です.
"""


def format_enter_instruction(anger_phrase: str = "") -> str:
    """反省モード突入時に additionalContext 先頭に置く instruction."""
    if anger_phrase:
        return f"{_ENTER_INSTRUCTION}\n_(検知フレーズ: 「{anger_phrase}」)_"
    return _ENTER_INSTRUCTION


def format_continue_instruction(turn: int) -> str:
    """反省モード継続中の毎ターン注入."""
    return _CONTINUE_INSTRUCTION.format(turn=turn)
