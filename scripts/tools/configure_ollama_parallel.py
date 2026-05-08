#!/usr/bin/env python3
"""ホスト RAM に基づいて OLLAMA_NUM_PARALLEL の最適値を自動設定する.

setup.sh から呼ばれる。

挙動:
- RAM サイズを検出 (macOS: sysctl hw.memsize / Linux: /proc/meminfo)
- RAM に応じた推奨値を算出
- ~/.zshrc (or ~/.bashrc) に marker 付きブロックで `export OLLAMA_NUM_PARALLEL=<n>` を追加
  - 既存ブロックは値を上書き、無ければ末尾に追加
  - マーカー外のユーザー設定は触らない
- 適用方法をユーザーに案内 (`source ~/.zshrc` + ollama 再起動)

ユーザーが手動で plist 等で別管理している場合はそちらも案内する。
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

START_MARKER = "# >>> persona-memory: OLLAMA_NUM_PARALLEL >>>"
END_MARKER = "# <<< persona-memory: OLLAMA_NUM_PARALLEL <<<"

_BLOCK_RE = re.compile(
    re.escape(START_MARKER) + r".*?" + re.escape(END_MARKER) + r"\n?",
    re.DOTALL,
)


def detect_ram_gb() -> int:
    """ホストの物理 RAM を GB 単位で返す. 検出失敗時は 16 と仮定."""
    if sys.platform == "darwin":
        try:
            out = subprocess.check_output(
                ["sysctl", "-n", "hw.memsize"], timeout=5,
            ).decode().strip()
            return int(int(out) / (1024 ** 3))
        except Exception:
            return 16
    if sys.platform.startswith("linux"):
        try:
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        kb = int(line.split()[1])
                        return int(kb / (1024 ** 2))
        except Exception:
            pass
    return 16


def recommend_parallel(ram_gb: int) -> int:
    """RAM 容量帯から OLLAMA_NUM_PARALLEL の推奨値を出す.

    閾値の根拠 (大まかな目安):
    - 各 inference 並列はモデル本体メモリは共有するが、KV cache は別。
      light (gemma3:4b) で 1 並列あたり 1-2GB、heavy (gemma3:12b) で 3-4GB の
      上乗せが目安。複数 persona 同時起動も考慮して保守的に。
    """
    if ram_gb < 12:
        return 2
    if ram_gb < 24:
        return 4
    if ram_gb < 48:
        return 6
    return 8


def _resolve_rc_path() -> Path:
    """書き込む shell rc ファイルを決定. zsh 優先、無ければ bash."""
    home = Path.home()
    shell = os.environ.get("SHELL", "")
    if "zsh" in shell:
        return home / ".zshrc"
    if "bash" in shell:
        for cand in (home / ".bashrc", home / ".bash_profile"):
            if cand.exists():
                return cand
        return home / ".bashrc"
    # default: zshrc (macOS の現代デフォルト)
    return home / ".zshrc"


def write_block(rc_path: Path, value: int) -> str:
    """marker 付きブロックを rc に書く. 戻り値: 'created' / 'updated' / 'appended' / 'unchanged'."""
    block = (
        f"{START_MARKER}\n"
        f"# Auto-set by persona-memory plugin (setup.sh).\n"
        f"# 複数プロジェクト/人格を同時起動した時に Ollama のリクエストキューを並列化する。\n"
        f"# 不要なら以下 3 行を削除すれば無効化される。\n"
        f"export OLLAMA_NUM_PARALLEL={value}\n"
        f"{END_MARKER}\n"
    )

    if not rc_path.exists():
        rc_path.write_text(block, encoding="utf-8")
        return "created"

    text = rc_path.read_text(encoding="utf-8")
    if _BLOCK_RE.search(text):
        # lambda で渡し re.sub の backref 解釈を回避
        new_text = _BLOCK_RE.sub(lambda _m: block, text)
        if new_text == text:
            return "unchanged"
        rc_path.write_text(new_text, encoding="utf-8")
        return "updated"

    sep = "" if text.endswith("\n") else "\n"
    rc_path.write_text(text + sep + "\n" + block, encoding="utf-8")
    return "appended"


def main() -> int:
    p = argparse.ArgumentParser(
        description="OLLAMA_NUM_PARALLEL を RAM サイズから自動設定"
    )
    p.add_argument("--dry-run", action="store_true",
                   help="rc ファイルに書かず推奨値だけ表示")
    args = p.parse_args()

    ram = detect_ram_gb()
    rec = recommend_parallel(ram)

    print(f"detected RAM:           {ram}GB")
    print(f"recommended NUM_PARALLEL: {rec}")

    current = os.environ.get("OLLAMA_NUM_PARALLEL")
    if current:
        print(f"current OLLAMA_NUM_PARALLEL: {current}")

    if args.dry_run:
        print("(dry-run: rc は書き換えない)")
        return 0

    rc = _resolve_rc_path()
    status = write_block(rc, rec)
    print(f"{rc}: {status}")

    if status in ("created", "appended", "updated"):
        print()
        print("適用方法:")
        print(f"  source {rc}")
        print("  pkill -x ollama 2>/dev/null; sleep 1")
        print(f"  OLLAMA_NUM_PARALLEL={rec} ollama serve >/dev/null 2>&1 &")
        print()
        print("(brew launchd / plist 等で Ollama を管理している場合は、")
        print(" plist の EnvironmentVariables に同じ値を入れて launchctl unload/load")
        print(" で再起動してください)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
