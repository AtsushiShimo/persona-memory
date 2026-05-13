"""機密検出のテスト。"""
from __future__ import annotations

from scripts.secrets.detect import detect_secrets, warning_message


def test_clean_text():
    assert detect_secrets("普通の会話。コーヒーは深煎りが好き。") == []
    assert detect_secrets("") == []


def test_openai_key():
    text = "API キーは sk-proj-abcdefghijklmnopqrstuvwxyz1234567890 です"
    found = detect_secrets(text)
    assert len(found) == 1
    assert found[0].startswith("sk-pro")
    assert found[0].endswith("7890")


def test_github_pat():
    text = "ghp_" + "x" * 40
    found = detect_secrets(text)
    assert len(found) == 1


def test_aws_key():
    text = "AWS: AKIAIOSFODNN7EXAMPLE"
    found = detect_secrets(text)
    assert len(found) == 1


def test_jwt():
    text = "Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NSJ9.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    found = detect_secrets(text)
    assert len(found) == 1


def test_high_entropy_token():
    """既知パターンに当たらない高エントロピー文字列も検出。"""
    # 32 文字以上のランダム ASCII
    token = "Xq7vK9pL3mN8wR2tY5sH1jD4gF6cZ0aB"
    text = f"トークン: {token}"
    found = detect_secrets(text)
    assert len(found) == 1


def test_low_entropy_long_string_not_flagged():
    """低エントロピー (繰り返し) は誤検出しない。"""
    text = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    found = detect_secrets(text)
    assert found == []


def test_warning_message_contains_keychain_guidance():
    msg = warning_message(["sk-pro...7890"])
    assert "機密" in msg
    assert ".env" in msg  # Keychain ガイダンス
    assert "sk-pro...7890" in msg


# ── 0.6.21: URL 誤検知防止 ─────────────────────────────────────────────

def test_x_url_with_tracking_params_not_flagged():
    """X (Twitter) URL の追跡パラメータは機密ではない (0.6.20 で誤検知発生)."""
    text = (
        "この記事参考に: "
        "https://x.com/milbon_/status/2054007474836168826?s=46&t=wlf1WBSt4VI0P_ni__K4Lw "
        "を読んで考えた."
    )
    assert detect_secrets(text) == []


def test_long_url_path_not_flagged():
    """長い URL path も誤検知しない."""
    text = (
        "github の長いコミット URL: "
        "https://github.com/AtsushiShimo/persona-memory/commit/0123456789abcdef0123456789abcdef01234567 "
        "を確認."
    )
    assert detect_secrets(text) == []


def test_api_key_inside_url_still_flagged():
    """URL を剥がしても URL 内に埋め込まれた API キーは既知パターンで検出される."""
    text = (
        "間違って公開: "
        "https://example.com/api?token=sk-proj-abcdefghijklmnopqrstuvwxyz1234567890"
    )
    found = detect_secrets(text)
    assert len(found) == 1
    assert found[0].startswith("sk-pro")


def test_high_entropy_outside_url_still_flagged():
    """URL 外の高エントロピートークンは引き続き検出する."""
    token = "Xq7vK9pL3mN8wR2tY5sH1jD4gF6cZ0aB"
    text = (
        f"参考 URL: https://example.com/foo/bar . でも秘密のトークン: {token}"
    )
    found = detect_secrets(text)
    assert len(found) == 1
