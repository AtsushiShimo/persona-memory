"""反省モード instruction text の必須要素テスト (0.8.6 反省モード仕様改修).

仕様:
- 入った瞬間にペルソナ口調で明示宣言する文言が必ず含まれる
- 継続中は応答頭にバナーを出させる文言が必ず含まれる
- 話題切替による自動解除は廃止されたので mention されない
"""
from __future__ import annotations

from scripts.reflection.instruction import (
    format_continue_instruction, format_enter_instruction,
)


def test_enter_instruction_requires_persona_voice_declaration():
    """入った瞬間、 ペルソナ口調での明示宣言が冒頭発話として要求される."""
    text = format_enter_instruction()
    assert "反省モードに入らせていただきます" in text


def test_enter_instruction_includes_apology():
    """宣言と同時に謝罪を要求."""
    text = format_enter_instruction()
    assert "申し訳" in text


def test_enter_instruction_passes_anger_phrase_through():
    """検知フレーズが渡された場合は付録される (既存挙動の維持)."""
    text = format_enter_instruction("ふざけるな")
    assert "ふざけるな" in text


def test_continue_instruction_requires_visible_banner():
    """継続中の応答頭に「🔴 反省モード継続中」 バナーを出させる指示."""
    text = format_continue_instruction(turn=3)
    assert "🔴" in text
    assert "反省モード継続中" in text


def test_continue_instruction_does_not_mention_topic_switch_release():
    """話題切替による自動解除は廃止. instruction にも書かない."""
    text = format_continue_instruction(turn=3)
    assert "話題を切り替え" not in text
    assert "話題切替" not in text


def test_continue_instruction_shows_turn_number():
    """ターン番号は引き続き表示."""
    text = format_continue_instruction(turn=7)
    assert "7" in text
