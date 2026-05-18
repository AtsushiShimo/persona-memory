"""デバッグモード state 管理 (= ユーザー判断による DB 直接アクセス許可).

背景: 0.7.7 まで デバッグモード切替は環境変数 PERSONA_MEMORY_DEBUG のみで、
セッション維持したまま toggle できなかった (= プロセス再起動が必要). 不具合の
デバッグで「DB を一時的に覗きたい」 場面でセッションを破棄するのは UX 悪い.

設計:
- ファイルベース flag (.persona-memory/debug_mode.flag) を採用.
  内容: "expires_at:<unix_ts>\\nreason:<str>" の plain text.
- hook (= on_pre_tool_use.py) は flag 存在 + 有効期限内 なら DB block を skip.
- TTL 切れ flag は hook 側で自己清掃 (= 切り忘れ防止セーフティネット).
- toggle は MCP tool `set_debug_mode(on, ttl_seconds, reason)` 経由のみ.
  main agent (= ペルソナ) が「ご主人様のデバッグ指示」 と判断した時に呼ぶ.

設計の根拠:
- Cozo 経路に乗せると hook 毎発火で Cozo 接続コストが発生する.
- ファイル存在チェック (os.path.exists) は ~1 μs で hook latency に影響なし.
- 内容 read も TTL チェック時のみ (= flag 存在時のみ) で全体軽量.
- 環境変数 PERSONA_MEMORY_DEBUG との OR 評価: 既存の plugin 開発者用経路と
  併存させて壊さない.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

FLAG_FILENAME = "debug_mode.flag"
DEFAULT_TTL_SECONDS = 1800  # 30 分


def _flag_path(db_path: Path) -> Path:
    """SQLite path から flag path を導出 (= 同じ .persona-memory ディレクトリ)."""
    return db_path.parent / FLAG_FILENAME


def is_active(db_path: Path) -> bool:
    """デバッグモードが現在 active か.

    判定:
    - flag ファイル不在 → False
    - flag 内 expires_at が現在時刻より過去 → False (+ 自己清掃)
    - それ以外 → True
    """
    flag = _flag_path(db_path)
    if not flag.exists():
        return False
    try:
        text = flag.read_text(encoding="utf-8")
        expires_at = 0
        for line in text.splitlines():
            if line.startswith("expires_at:"):
                try:
                    expires_at = int(line.split(":", 1)[1].strip())
                except ValueError:
                    expires_at = 0
                break
        if expires_at <= 0 or time.time() > expires_at:
            # TTL 切れ → 自己清掃
            try:
                flag.unlink()
            except FileNotFoundError:
                pass
            return False
        return True
    except Exception:
        return False


def enable(
    db_path: Path,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    reason: str = "",
) -> dict:
    """flag を書き出してデバッグモード on.

    既に on の場合は expires_at を延長 (= 上書き).
    """
    if ttl_seconds <= 0:
        ttl_seconds = DEFAULT_TTL_SECONDS
    expires_at = int(time.time()) + int(ttl_seconds)
    flag = _flag_path(db_path)
    flag.parent.mkdir(parents=True, exist_ok=True)
    body = f"expires_at:{expires_at}\nreason:{reason or ''}\n"
    flag.write_text(body, encoding="utf-8")
    return {
        "active": True,
        "expires_at": expires_at,
        "ttl_seconds": ttl_seconds,
        "reason": reason or "",
    }


def disable(db_path: Path) -> dict:
    """flag を削除してデバッグモード off."""
    flag = _flag_path(db_path)
    try:
        flag.unlink()
        return {"active": False, "removed": True}
    except FileNotFoundError:
        return {"active": False, "removed": False}


def status(db_path: Path) -> dict:
    """現在の状態を dict で返す (確認用)."""
    flag = _flag_path(db_path)
    if not flag.exists():
        return {"active": False}
    if not is_active(db_path):
        return {"active": False, "expired": True}
    try:
        text = flag.read_text(encoding="utf-8")
        info: dict = {"active": True}
        for line in text.splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                info[k.strip()] = v.strip()
        if "expires_at" in info:
            try:
                exp = int(info["expires_at"])
                info["remaining_seconds"] = max(0, exp - int(time.time()))
            except ValueError:
                pass
        return info
    except Exception:
        return {"active": True}


def is_env_debug() -> bool:
    """環境変数 PERSONA_MEMORY_DEBUG が立っているか (旧経路)."""
    return bool(os.environ.get("PERSONA_MEMORY_DEBUG", "").strip())


def is_debug_active(db_path: Path | None) -> bool:
    """flag or 環境変数 のどちらかで debug 有効か (hook が呼ぶ)."""
    if is_env_debug():
        return True
    if db_path is None:
        return False
    return is_active(db_path)
