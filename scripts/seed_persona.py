#!/usr/bin/env python3
"""新スキーマ向けに persona facts を seed する。

scripts/init.sh から呼ばれ、setup.sh が venv + DB を作った後に実行される。
新モジュール (scripts.db / scripts.shared.ollama / scripts.shared.embedding) を
使って seed する。Ollama 未起動時は embedding なしで保存 (recall は弱まるが
SessionStart の boot 層注入には影響しない)。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.boot.defaults import DEFAULT_BOOT_FACTS
from scripts.db.connection import connect
from scripts.shared.embedding import pack
from scripts.shared.ollama import OllamaClient

EMBED_MODEL = os.environ.get("PERSONA_EMBED_MODEL", "nomic-embed-text")

# 立ち位置 5 軸 (init で対話入力, -2..+2 の整数).
# 設計意図: 複数ペルソナを使う時に思考傾向を意図的に散らして, 視野狭窄を
# 破壊する役割を担わせる. boot 層 persona/stance に注入して応答スタイルに反映.
STANCE_AXES = [
    # (左ラベル, 右ラベル, 左寄り行動指針, 右寄り行動指針)
    ("保守", "革新",
     "実績ある手段や既存パターンを優先し、 不確実な提案は避ける",
     "新しい案や別解を積極的に提案し、 別の解決経路も提示する"),
    ("楽観", "悲観",
     "うまくいく前提で前向きに提案し、 可能性を強調する",
     "リスク・落とし穴・失敗パターンを先に挙げる"),
    ("直感", "分析",
     "経験則や勘で素早く方向を示し、 詳細検証は後回し",
     "根拠・データ・仕様を先に示してから結論を述べる"),
    ("慎重", "大胆",
     "確認・段階分割・小さな実験を重ねて手堅く進める",
     "リスクは挙げるが踏み込んだ提案を先に出す"),
    ("共感", "論理",
     "ユーザーの状況・感情・意図に配慮した言い回しを優先する",
     "感情に流されず論理・整合性・原理原則を最優先で貫く"),
]


def _stance_to_natural_language(values: list[int]) -> str:
    """5 軸の値 (-2..+2) を persona/stance の自然語 value に変換.

    LLM が応答スタイルに翻訳しやすいよう、 各軸を「タグ — 行動指針」 形式
    で記述し、 末尾に dominant 軸 (|値| >= 2) のまとめを置く.
    """
    lines = ["あなたの立ち位置 (応答スタイルに反映):"]
    for v, (left, right, left_dir, right_dir) in zip(values, STANCE_AXES):
        if v <= -2:
            tag = f"強く{left}寄り"
            dir_text = left_dir
        elif v == -1:
            tag = f"やや{left}寄り"
            dir_text = left_dir
        elif v == 0:
            tag = "バランス"
            dir_text = f"{left}と{right}を状況に応じて使い分ける"
        elif v == 1:
            tag = f"やや{right}寄り"
            dir_text = right_dir
        else:
            tag = f"強く{right}寄り"
            dir_text = right_dir
        lines.append(f"- {left}-{right}軸: {tag} — {dir_text}")
    dominant: list[str] = []
    for v, (left, right, _, _) in zip(values, STANCE_AXES):
        if v <= -2:
            dominant.append(left)
        elif v >= 2:
            dominant.append(right)
    if dominant:
        lines.append("")
        lines.append(f"総合: {' / '.join(dominant)} の傾向が特に強い.")
    return "\n".join(lines)


def _parse_stance_csv(s: str) -> list[int]:
    """`--stance "0,1,-1,2,-1"` をパースして 5 要素 int list に.

    フォーマット不正 (要素数違い / 整数化失敗 / 範囲外) で ValueError.
    """
    parts = [p.strip() for p in s.split(",")]
    if len(parts) != len(STANCE_AXES):
        raise ValueError(
            f"--stance は {len(STANCE_AXES)} 要素必要 (受信: {len(parts)})"
        )
    out: list[int] = []
    for p in parts:
        try:
            v = int(p)
        except ValueError:
            raise ValueError(f"--stance の要素が整数でない: {p!r}")
        if v < -2 or v > 2:
            raise ValueError(f"--stance の要素は -2..+2 (受信: {v})")
        out.append(v)
    return out


def _normalize_address_user(raw: str) -> str:
    """address_user 入力を正規化し、 LLM が name slot を捏造する誘惑を断つ.

    placeholder 風 (`〜` を含む or `(敬称)` を含む) の入力は、 LLM に
    「ここに名前を補え」 と解釈させる name slot として機能してしまう
    (実機検証で `simo さん` 等の捏造を観測). 正規化方針:
    - `〜さん (敬称)` のように `〜` を含むものは
      「『あなた』 (固有名詞は使わず汎用代名詞)」 に展開
    - `(敬称)` 等の補注は剥がす
    - 既に固有な呼称 (「マスター」「あなた」「君」「ユーザーさん」 等) はそのまま
    """
    s = raw.strip()
    if "〜" in s or "～" in s:
        return "『あなた』 (ユーザーの名前は知らないので呼ばない. 固有名詞でユーザーを呼びかけないこと)"
    if s.endswith(" (敬称)"):
        s = s[: -len(" (敬称)")].strip()
    return s


def _upsert_boot_fact(conn, category: str, key: str, value: str, importance: int) -> int:
    """boot 層 (persona / rule) に upsert (UNIQUE(category,key) WHERE active を使う)。"""
    row = conn.execute(
        "SELECT id FROM facts WHERE category=? AND key=? AND status='active'",
        (category, key),
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE facts SET value=?, importance=?, "
            "  updated_at=datetime('now', '+9 hours') "
            "WHERE id=?",
            (value, importance, row[0]),
        )
        return row[0]
    cur = conn.execute(
        "INSERT INTO facts(category, key, value, importance, source) "
        "VALUES (?, ?, ?, ?, 'init')",
        (category, key, value, importance),
    )
    return cur.lastrowid


def seed(
    db_path: Path,
    *,
    role: str,
    name: str,
    gender: str,
    personality: str,
    first_person: str,
    speech_style: str,
    address_user: str,
    stance: list[int] | None = None,
) -> None:
    # ペルソナ固有属性 (init の質問回答から組み立てる) +
    # 共通 default 行動指針 (scripts/boot/defaults.py から import)
    user_facts: list[tuple[str, str, str, int]] = [
        ("persona", "role", role, 9),
        ("persona", "identity", f"このペルソナの名前は『{name}』", 9),
        ("persona", "personality", personality, 9),
        ("persona", "gender", f"性別: {gender}", 8),
        ("persona", "first_person", f"一人称は『{first_person}』", 8),
        ("persona", "speech_style", speech_style, 8),
        ("persona", "address_user", _normalize_address_user(address_user), 8),
    ]
    if stance is not None and len(stance) == len(STANCE_AXES):
        user_facts.append(("persona", "stance", _stance_to_natural_language(stance), 9))
    facts = user_facts + DEFAULT_BOOT_FACTS

    conn = connect(db_path)
    client = OllamaClient()
    try:
        for category, key, value, importance in facts:
            fid = _upsert_boot_fact(conn, category, key, value, importance)
            conn.commit()
            try:
                vec = client.embed(EMBED_MODEL, f"{category}/{key}: {value}")
                if vec:
                    conn.execute(
                        "INSERT OR REPLACE INTO fact_embeddings(fact_id, embedding) "
                        "VALUES (?, ?)",
                        (fid, pack(vec)),
                    )
                    conn.commit()
                    print(f"  seeded [{category}/{key}] (importance={importance})")
                else:
                    print(
                        f"  WARN seed [{category}/{key}] saved without embedding "
                        f"(empty vector)",
                        file=sys.stderr,
                    )
            except Exception as e:
                print(
                    f"  WARN seed [{category}/{key}] saved without embedding: {e}",
                    file=sys.stderr,
                )
    finally:
        conn.close()


def main() -> None:
    p = argparse.ArgumentParser(description="Seed initial persona facts.")
    p.add_argument("--db", type=Path, default=None,
                   help="DB ファイルパス (未指定時は PERSONA_MEMORY_DB env を使う)")
    p.add_argument("--role", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--gender", required=True)
    p.add_argument("--personality", required=True)
    p.add_argument("--first-person", required=True)
    p.add_argument("--speech-style", required=True)
    p.add_argument("--address-user", required=True)
    p.add_argument(
        "--stance",
        default=None,
        help='立ち位置 5 軸 CSV "v1,v2,v3,v4,v5" (各 -2..+2). '
             '軸順: 保守-革新 / 楽観-悲観 / 直感-分析 / 慎重-大胆 / 共感-論理. '
             '省略時は persona/stance を seed しない (後方互換).',
    )
    args = p.parse_args()

    stance_values: list[int] | None = None
    if args.stance:
        try:
            stance_values = _parse_stance_csv(args.stance)
        except ValueError as e:
            print(f"ERROR: --stance: {e}", file=sys.stderr)
            sys.exit(2)

    db_path = args.db
    if db_path is None:
        env_db = os.environ.get("PERSONA_MEMORY_DB", "").strip()
        if not env_db:
            print("ERROR: --db か PERSONA_MEMORY_DB env のどちらかが必要です", file=sys.stderr)
            sys.exit(2)
        db_path = Path(env_db)

    seed(
        db_path,
        role=args.role,
        name=args.name,
        gender=args.gender,
        personality=args.personality,
        first_person=args.first_person,
        speech_style=args.speech_style,
        address_user=args.address_user,
        stance=stance_values,
    )


if __name__ == "__main__":
    main()
