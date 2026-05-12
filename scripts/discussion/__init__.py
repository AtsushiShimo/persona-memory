"""議論グラフ (Discussion Graph) 操作モジュール (0.6.13 Phase A1).

agentmemory の静的知識グラフを「時系列・状態遷移を持つ DAG」 に進化させた
構造ナビゲーション基盤。 ノード = 論点 / 検討案 / 採用判断 / 撤回理由、
エッジ = 派生 / 検討 / 決定 / 撤回 / 上書き / 前提.

Phase A1: 基本 CRUD + 状態遷移 helper のみ. write LLM 統合は Phase A2、
recall 検索組み込みは Phase B で別 commit.
"""
