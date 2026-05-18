"""過去叱責 (HANDOFF.md Section 14.5) の lesson + trigger 一括 seed.

呼び出し: `/persona-memory:seed-lessons` slash command 経由でユーザーが明示
発火する. 反省モード経路を介さない backfill だが、 ユーザーの明示実行 = 承認.

各 lesson は category='lesson' + key 安定識別子で write される. 既存 lesson が
あれば fact_persist の apply_candidate ロジックで supersede される
(value 変更時) or 再強化 (= 同 value reinforce).

trigger は lesson_trigger relation に register_trigger で追加. 同 lesson に
対する登録は delete_triggers_for で旧 trigger を一掃してから上書きする
(= 冪等).
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field

from pycozo.client import Client

from scripts.db_cozo.connection import init_db, next_id
from scripts.reflection.lesson import (
    delete_triggers_for, get_lesson_by_key, register_trigger,
)


@dataclass
class LessonSeed:
    key: str
    value: str
    importance: int = 10
    # 各 trigger は (kind, pattern, action) の tuple
    triggers: list[tuple[str, str, str]] = field(default_factory=list)


# Section 14.5 の 11 項を構造化. value は元文章を 1-2 文に圧縮.
# trigger は「いつ思い出させるか」 を最大保守的に設定 (= 取りこぼし防止優先).
PAST_LESSONS: list[LessonSeed] = [
    # 14.5.1 キャッシュ編集禁止
    LessonSeed(
        key="cache_edit_block",
        value=(
            "~/.claude/plugins/cache/ 配下のファイルを編集してはならない. "
            "改修は元 repo (= 開発リポ) のみ. キャッシュ編集は次回 plugin "
            "更新で消失し元 repo に反映されない. キャッシュ Read も警告対象."
        ),
        triggers=[
            ("path_edit", r"\.claude/plugins/cache/", "block"),
            ("path_read", r"\.claude/plugins/cache/", "warn"),
            ("bash_cmd", r"\.claude/plugins/cache/", "warn"),
        ],
    ),
    # 14.5.2 既存ソース未読で設計
    LessonSeed(
        key="read_existing_source_before_design",
        value=(
            "設計案を出す前に関連ファイルを全文読む (部分読み禁止). 既存実装の "
            "呼び出し経路を「誰がどこから呼ぶか」 列挙してから設計に入る. "
            "ショートカット禁止."
        ),
        triggers=[
            ("prompt_intent", r"設計|スキーマ|新.{0,4}機能|構造化|アーキ",
             "warn"),
        ],
    ),
    # 14.5.3 最小単位で「動きました」 と完成扱い
    LessonSeed(
        key="no_partial_completion_report",
        value=(
            "合意済み設計の全要素が動くまで完成報告しない. 最小単位 (Phase 1 等) "
            "で「動きました」 と報告するのは禁止."
        ),
        triggers=[
            ("prompt_intent", r"完成|完了|動きました|出来ました|終わ", "warn"),
        ],
    ),
    # 14.5.4 段階を勝手に分けた
    LessonSeed(
        key="no_arbitrary_phase_split",
        value=(
            "マスター指示の範囲 = 1 つの完成単位. 勝手に「次段階」 と分けて "
            "途中報告するのは禁止. commit 分割はマスターが判断する."
        ),
        triggers=[
            ("prompt_intent", r"Phase|フェーズ|次段階|分割|段階", "warn"),
        ],
    ),
    # 14.5.5 backfill 軽視と過剰適用
    LessonSeed(
        key="realtime_first_backfill_parallel",
        value=(
            "第一義はリアルタイム経路. リアルタイムを改修したら必ず backfill にも "
            "同じ実装を反映する (共有関数で). backfill は下位互換 + 過去議論を "
            "使ったテストの 2 役割."
        ),
        triggers=[
            ("prompt_intent", r"backfill|遡及|リアルタイム", "warn"),
        ],
    ),
    # 14.5.6 ユーザーに手動コマンドを叩かせる設計
    LessonSeed(
        key="no_manual_command_dispatch",
        value=(
            "backfill / migration / 救済処理は必ず hook 経路で自動発火する. "
            "ユーザーに `python -m ...` を叩かせる設計は禁止. 多重起動防止は "
            "lockfile, 制御は env opt-out のみ."
        ),
        triggers=[
            ("prompt_intent", r"手動|python -m|実行してください|叩いて", "warn"),
        ],
    ),
    # 14.5.7 解釈ズレで動いた
    LessonSeed(
        key="ask_when_intent_ambiguous",
        value=(
            "ユーザーの言葉が複数解釈可能な時は動かずに質問する. 推測で押し "
            "進めるのは禁止. 認識合わせのコストは安く、 暴走のコストは高い."
        ),
        triggers=[
            ("prompt_intent",
             r"ちげー|違うよ|認識.{0,4}違|そういう意味じゃ|そうじゃない",
             "warn"),
        ],
    ),
    # 14.5.8 指示を忘れた
    LessonSeed(
        key="persist_user_directives_immediately",
        value=(
            "ユーザーの「次やること」 系の明示指示は即 TODO 化して記憶に刻む. "
            "セッション終盤や commit 後に「次やること」 を確認する流れを確立."
        ),
        triggers=[
            ("prompt_intent",
             r"次の指示|次やること|覚えて|忘れるな|この指示", "warn"),
        ],
    ),
    # 14.5.9 オーナーチェックを求めた
    LessonSeed(
        key="self_test_before_report",
        value=(
            "自分のミスのリカバリは自分でテストして保証してから報告する. "
            "ユーザーに動作確認を依頼するのは禁止 (= 最終受容判断のみ)."
        ),
        triggers=[
            ("prompt_intent",
             r"動作確認|チェックして|確認して.{0,4}ください|テストして.{0,4}ください",
             "warn"),
            ("bash_cmd", r"^\s*git\s+(commit|push)", "warn"),
        ],
    ),
    # 14.5.10 冗長な前置き・報告過多
    LessonSeed(
        key="response_brevity_enforce",
        value=(
            "状況再確認 + 進捗報告 + 提案 + 確認を 1 応答に詰め込まない. "
            "端的に, 核だけ即答, 補足は 1-2 行, 複数案は明示要求時のみ."
        ),
        triggers=[
            ("general", r".", "warn"),  # 全発話で軽い注意喚起
        ],
    ),
    # 14.5.11 インプ稼ぎを真面目に追った
    LessonSeed(
        key="suspect_clickbait_tone",
        value=(
            "煽り口調 (「公式が暴露」「別ゲー」「数ヶ月後広がる」「ブクマ推奨」 等) "
            "は典型的インプ稼ぎテンプレ. 中身を確認する前に文体で判別する目を持つ."
        ),
        triggers=[
            ("prompt_intent",
             r"公式が暴露|別ゲー|ブクマ推奨|数ヶ月後広がる|革命的",
             "warn"),
        ],
    ),
]


def _now_jst() -> str:
    import datetime as dt
    tz = dt.timezone(dt.timedelta(hours=9))
    return dt.datetime.now(tz).replace(tzinfo=None).isoformat(
        sep=" ", timespec="seconds",
    )


def _upsert_lesson_in_cozo(
    client: Client, seed: LessonSeed,
) -> int:
    """lesson fact (category='lesson', key=seed.key) を Cozo に upsert.

    既存 fact があれば value 更新 (= reinforce 相当). 無ければ新規 insert.
    returns: fact_id
    """
    existing = get_lesson_by_key(client, seed.key)
    ts = _now_jst()
    if existing is not None:
        client.run(
            "?[id, category, key, value, importance, status, created_at, "
            "updated_at, access_count] <- "
            "[[$id, 'lesson', $key, $value, $imp, 'active', $ts, $ts, 0]] "
            ":put fact {id => category, key, value, importance, status, "
            "created_at, updated_at, access_count}",
            {"id": existing.fact_id, "key": seed.key,
             "value": seed.value, "imp": seed.importance, "ts": ts},
        )
        return existing.fact_id
    new_id = next_id(client, "fact")
    client.run(
        "?[id, category, key, value, importance, status, created_at, "
        "updated_at, access_count] <- "
        "[[$id, 'lesson', $key, $value, $imp, 'active', $ts, $ts, 0]] "
        ":put fact {id => category, key, value, importance, status, "
        "created_at, updated_at, access_count}",
        {"id": new_id, "key": seed.key,
         "value": seed.value, "imp": seed.importance, "ts": ts},
    )
    return new_id


def seed_all(db_path) -> dict:
    """全 PAST_LESSONS を Cozo に書き込み. 冪等. 結果サマリ dict を返す."""
    from pathlib import Path as _Path
    client = init_db(_Path(db_path))
    written = 0
    triggers_total = 0
    for seed in PAST_LESSONS:
        fact_id = _upsert_lesson_in_cozo(client, seed)
        delete_triggers_for(client, fact_id)
        for (kind, pattern, action) in seed.triggers:
            register_trigger(client, fact_id, kind, pattern, action)
            triggers_total += 1
        written += 1
    return {
        "lessons_written": written,
        "triggers_registered": triggers_total,
    }


def main() -> int:
    """CLI: PERSONA_MEMORY_DB に対して seed 実行."""
    import os
    db = os.environ.get("PERSONA_MEMORY_DB")
    if not db:
        sys.stderr.write("PERSONA_MEMORY_DB 未設定\n")
        return 1
    from pathlib import Path
    p = Path(db)
    # 旧 SQLite path (`<persona>.db`) を受けても `.cozo.db` に振り直す.
    if p.suffix == ".db" and not p.name.endswith(".cozo.db"):
        p = p.with_suffix(".cozo.db")
    if not p.exists():
        sys.stderr.write(
            f"Cozo DB が見つかりません ({p}). "
            "/persona-memory:init でペルソナを作成してください.\n"
        )
        return 1
    result = seed_all(p)
    sys.stdout.write(
        f"lesson 書き込み: {result['lessons_written']} 件 / "
        f"trigger 登録: {result['triggers_registered']} 件\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
