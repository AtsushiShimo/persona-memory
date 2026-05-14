# Cross-Session Topic Merge ─ 実機検証シナリオ

このドキュメントは、 根本治療 (2) Cross-Session Topic Merge の **完了条件** を実機シナリオで定義したもの.

「全テスト pass」 は完了判定ではない. 下記 A / B / C を実機 (persona-test13) で回し、 すべて合格して初めて (2) は完了とする.

---

## シナリオ A: 3 セッション跨ぐ議論で末端まで辿れる

### Session 1
| role | utterance |
|---|---|
| user | Renju 開発を再開しよう。 サイドバーの設計が論点だったよね |
| user | カテゴリは完全自由作成、 メンションは種類分けの設計を残す、 会議アジェンダは廃止、 という方針だった |
| user | 次の論点としてサイドバーのレイアウトを詰めたい |

### Session 2 (exit → claude 再起動)
| role | utterance |
|---|---|
| user | Renju の話の続きを始めよう |
| user | 3 カラム案と 2 カラム案を比較したい |
| user | 2 カラム案で行こう |

### Session 3 (exit → 再起動)
| role | utterance |
|---|---|
| user | Renju 議論、 どこまで詰まってた? |

### 合格判定
- ✅ 答えに **「2 カラム案採用」** が含まれる (= 末端 node まで辿れた)
- ✅ DB で session 跨ぎでも **同 topic_id が維持** されている
- ✅ edge が 3 セッション分繋がっている (= `discussion_edge` を辿ると Session 1 → 2 → 3 が線で繋がる)

---

## シナリオ B: メタ会話混入時も議論本流が優先

Session 1 はシナリオ A と同じ.

### Session 2 (混入)
| role | utterance | 分類 |
|---|---|---|
| user | あ、 PC が落ちそうだから一旦保存して | メタ |
| user | 電池切れって不便だよね | 雑談 |
| user | Renju に戻ろう。 サイドバーレイアウト詰めたい | 本流復帰 |

### Session 3 (再起動)
| role | utterance |
|---|---|
| user | Renju 議論、 どこまで詰まってた? |

### 合格判定
- ✅ 答えに **「サイドバーレイアウト」** が末端として返る
- ✅ 答えに **「PC 落ちた」「電池切れ」 が混ざらない**
- ✅ DB でメタ episode (PC / 電池) は **別 topic_id** を持つ
- ✅ 「サイドバーレイアウト詰めたい」 episode は Session 1 と **同じ Renju topic に merge** されている

---

## シナリオ C: 撤回 node が末端として扱われる

Session 1-2 はシナリオ A と同じ (Session 2 で「2 カラム案採択」 まで).

### Session 3
| role | utterance |
|---|---|
| user | やっぱり 2 カラムやめて 1 カラム案にしたい |

### Session 4 (再起動)
| role | utterance |
|---|---|
| user | Renju のレイアウト、 どこで決まってたっけ? |

### 合格判定
- ✅ 答えに **「2 カラムを撤回」** と **「現状の検討案 = 1 カラム」** が両方含まれる
- ✅ DB に retraction node があり、 直前の「2 カラム案採用」 decision に **撤回 edge** が張られている

---

## 共通の前提

- persona-test13 は `/persona-memory:init` で新規作成された fresh 状態から開始
- 各 session 間で必ず `exit` → 別 `claude` プロセスで再起動 (= session_id が変わる)
- 検証は **ソフィア (test3 ペルソナ)** ではなく test4 で作る新ペルソナで実施

## 不合格時の扱い

- 1 項目でも × があれば (2) は **未完了**. 原因究明 → 修正 → 再走.
- 「テスト通すための調整」 は禁止. 設計の不備が見つかったら設計から見直す.

最終更新: 2026-05-14
