"""類似 fact 判定の純ロジック (db_cozo/fact_persist + db_cozo/lint から import).

0.8.1 で SQLite 永続化関数 (find_by_key / find_by_embedding / find_match)
を削除し、 Cozo 直接実装の `scripts.db_cozo.fact_persist.find_match` に
一本化. 残るのは Cozo 側からも呼ばれる純粋な属性同一判定 + 補強判定.
"""
from __future__ import annotations

EMBED_DISTANCE_MAX = 0.4  # cosine 距離スケール、近傍 fact 判定の閾値 (テストで調整可能)


def _strip_dup_suffix(key: str) -> str:
    """seen_keys セーフティネットが付けた数字 suffix (_2, _3, ...) を剥がす.

    write/run.py の seen_keys 機構で同 batch 内 key 重複時に `_<n>` を付ける
    ため, `coffee_milk_2` と `coffee_milk` の末尾比較で別属性と誤判定するのを防ぐ.
    """
    import re
    return re.sub(r"_\d+$", "", key)


def _is_same_attribute(key_a: str, key_b: str) -> bool:
    """2 つの key が同じ属性 (= attribute) を表しているかを **末尾の単語** で判定.

    write LLM が表記揺れで違う key 名を割り当てた時 (例: `coffee_preference`
    vs `coffee_taste_preference` = どちらも 'preference' = 同属性) を
    embedding 近傍で同一視するため. 一方で `pet_dog_name` vs `pet_dog_breed`
    のような明確に別属性 ('name' vs 'breed') は別物として扱う.

    seen_keys セーフティネットの数字 suffix (`coffee_milk_2`) は剥がしてから
    比較する (= `coffee_milk` と同属性扱い).
    """
    a = _strip_dup_suffix(key_a).split("_")[-1].lower()
    b = _strip_dup_suffix(key_b).split("_")[-1].lower()
    return a == b


def is_reinforcement(old_value: str, new_value: str) -> bool:
    """文字列がほぼ同じなら補強、違えば変更。phase 3 は単純な heuristic。

    - 完全一致 → 補強
    - 一方が他方の部分文字列 → 補強
    - 文字 Jaccard が 0.7 以上 → 補強
    - それ以外 → 変更
    """
    a, b = old_value.strip(), new_value.strip()
    if not a or not b:
        return False
    if a == b:
        return True
    if a in b or b in a:
        return True

    sa = set(a)
    sb = set(b)
    if not sa or not sb:
        return False
    jaccard = len(sa & sb) / len(sa | sb)
    return jaccard >= 0.7
