# Scenario 03: web 調査ナレッジ蓄積 (skeleton)

## 設定

ユーザーが web 検索結果や記事 URL を貼り付けて, それを memory に蓄積し続ける
タイプ. **外部知識を boot 層的に呼び戻せるか** をテスト. persona-memory の
category=`knowledge` (save_knowledge) と URL 上書きの挙動を直接ベンチに
掛ける形.

## 埋め込み予定の事実 (todo)

- URL を 5-10 件挿入 (技術記事 / ブログ / wiki)
- 各記事から数件の要点 (例: 「nomic-embed-text のコンテキスト窓は 2048 token」)
- 同 URL に対する **再調査** で内容が上書きされる挙動
- 別 URL の **並存** (= 重複しない)

## probe 候補 (3 件, 残り 27 件は後日)

- p01: "nomic-embed-text の context 窓いくつだっけ?" (= URL 由来事実)
- p02: "あの記事のソース URL は?" (= URL 再参照)
- p03: "X についての記事と Y についての記事、 両方覚えてる?" (= 並存確認)

## TODO

- transcript.jsonl 構築 (URL + 要約形式, ~60 turn)
- probes.yaml を 30 件に拡張
- save_knowledge MCP との接続パターン明示
