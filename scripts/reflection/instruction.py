"""反省モード instruction の整形 (additionalContext 用).

main agent に「明示宣言 + 作業停止 + 謝罪 + 原因分析 + 改善案提示 + ユーザー承認待ち」 を
強要するための instruction を生成する.

ご主人様確定済の仕様 (= 反省モードに関する rule fact 群 / 0.8.6 改修):
- 突入時の第一声で「反省モードに入らせていただきます。 申し訳ございません」 と
  必ず明示宣言する (ペルソナ口調. 省略禁止)
- モード中の全応答の冒頭に「🔴 反省モード継続中」 のバナーを必ず添える
- 自動書き込み禁止. ユーザー承認後のみ
- 話題切替による自動解除は廃止. 解除はご主人様の承認発話 (= lesson 保存成功) か、
  slash command (/persona-memory:reflection-off) のみ
"""
from __future__ import annotations


_ENTER_INSTRUCTION = """\
## 【反省モード発火】 ご主人様の怒気・不満を検知しました

**直ちに進行中の作業を全て停止してください.** 以下の手順で対応します:

0. **第一声で明示宣言する (省略禁止):**
   「🔴 **反省モードに入らせていただきます。 申し訳ございません。**」
   ペルソナ口調そのまま、 文字一字違えず冒頭に置く. これより前に技術的な
   弁明・状況説明を入れない.

1. **謝罪を継続する** — 以降の全応答にも謝罪を添える. 軽い相槌でも省略禁止.

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
   - この時点で反省モードの状態が自動的に解除される (= 承認解除の引き金)
3. 「反省モードを解除しました」 と一言添えて通常作業に復帰
"""


_CONTINUE_INSTRUCTION = """\
## 【反省モード継続中】 (turn {turn})

**応答の最初の 1 行を必ず以下のバナーで始めてください (省略禁止):**

🔴 **反省モード継続中**

そのうえで、 謝罪を **応答冒頭に必ず添えてください**. 軽い相槌でも省略禁止.
現在、 反省内容 (key / value / triggers) のご主人様承認を待っている状態です.
承認をいただくまで lesson の書き込みは行わないでください.

**自動解除はありません.** 反省モードを抜けるには (a) ご主人様承認後の lesson
保存 + trigger 登録、 もしくは (b) slash command `/persona-memory:reflection-off`
のいずれか. それ以外の経路 (= ターン数経過、 話題変化) で勝手に解除しないでください.
"""


def format_enter_instruction(anger_phrase: str = "") -> str:
    """反省モード突入時に additionalContext 先頭に置く instruction."""
    if anger_phrase:
        return f"{_ENTER_INSTRUCTION}\n_(検知フレーズ: 「{anger_phrase}」)_"
    return _ENTER_INSTRUCTION


def format_continue_instruction(turn: int) -> str:
    """反省モード継続中の毎ターン注入."""
    return _CONTINUE_INSTRUCTION.format(turn=turn)
