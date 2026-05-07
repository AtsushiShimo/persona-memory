"""Phase 7: デバッグモード (PERSONA_MEMORY_DEBUG) のテスト."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from scripts.db.connection import connect
from scripts.db.migrate import init_db
from scripts.debug import recall_log
from scripts.recall.run import recall
from scripts.write.extract import FactCandidate
from scripts.write.persist import insert_new


@dataclass
class FakeClient:
    keywords: list[str]
    embedding_map: dict[str, list[float]] = field(default_factory=dict)

    def generate(self, model, prompt):
        return json.dumps(
            {"keywords": self.keywords, "search_history": False},
            ensure_ascii=False,
        )

    def embed(self, model, text):
        return list(self.embedding_map.get(text, [0.0] * 768))


@pytest.fixture
def db(tmp_path: Path):
    db_path = tmp_path / "p.db"
    init_db(db_path)
    conn = connect(db_path)
    yield conn
    conn.close()


@pytest.fixture(autouse=True)
def _reset_env(monkeypatch):
    """各テストで env を初期化."""
    monkeypatch.delenv("PERSONA_MEMORY_DEBUG", raising=False)
    monkeypatch.delenv("PERSONA_DEBUG_LOG_PATH", raising=False)


def test_debug_disabled_by_default():
    assert recall_log.is_enabled() is False


def test_debug_enabled_with_1(monkeypatch):
    monkeypatch.setenv("PERSONA_MEMORY_DEBUG", "1")
    assert recall_log.is_enabled() is True


def test_debug_enabled_with_letter_levels(monkeypatch):
    for level in ("a", "b", "c"):
        monkeypatch.setenv("PERSONA_MEMORY_DEBUG", level)
        assert recall_log.is_enabled() is True


def test_debug_disabled_with_zero(monkeypatch):
    monkeypatch.setenv("PERSONA_MEMORY_DEBUG", "0")
    assert recall_log.is_enabled() is False


def test_log_keywords_writes_to_stderr_at_level_a(capsys, monkeypatch):
    monkeypatch.setenv("PERSONA_MEMORY_DEBUG", "a")
    recall_log.log_keywords("コーヒー好き?", [], ["コーヒー"])
    captured = capsys.readouterr()
    assert "recall.keywords" in captured.err
    assert "コーヒー" in captured.err


def test_log_hits_skipped_at_level_a(capsys, monkeypatch):
    monkeypatch.setenv("PERSONA_MEMORY_DEBUG", "a")
    recall_log.log_hits(1, [{"fact_id": 1}])
    captured = capsys.readouterr()
    assert "recall.hits" not in captured.err


def test_log_final_skipped_at_level_b(capsys, monkeypatch):
    monkeypatch.setenv("PERSONA_MEMORY_DEBUG", "b")
    recall_log.log_final_prompt("## 関連する記憶\n- ...")
    captured = capsys.readouterr()
    assert "recall.final" not in captured.err


def test_all_levels_logged_at_c(capsys, monkeypatch):
    monkeypatch.setenv("PERSONA_MEMORY_DEBUG", "c")
    recall_log.log_keywords("x", [], ["k"])
    recall_log.log_hits(1, [])
    recall_log.log_final_prompt("done")
    err = capsys.readouterr().err
    assert "recall.keywords" in err
    assert "recall.hits" in err
    assert "recall.final" in err


def test_log_writes_to_file_when_path_set(monkeypatch, tmp_path: Path):
    log_path = tmp_path / "debug.log"
    monkeypatch.setenv("PERSONA_MEMORY_DEBUG", "c")
    monkeypatch.setenv("PERSONA_DEBUG_LOG_PATH", str(log_path))
    recall_log.log_keywords("x", [], ["k"])
    assert log_path.exists()
    content = log_path.read_text(encoding="utf-8")
    assert "recall.keywords" in content
    assert "k" in content


def test_log_path_falls_back_to_db_dir(monkeypatch, tmp_path: Path):
    db = tmp_path / "p.db"
    db.touch()
    monkeypatch.setenv("PERSONA_MEMORY_DEBUG", "c")
    monkeypatch.setenv("PERSONA_MEMORY_DB", str(db))
    recall_log.log_keywords("x", [], ["k"])
    expected = tmp_path / "debug-recall.log"
    assert expected.exists()


def test_disabled_writes_nothing(capsys, monkeypatch, tmp_path: Path):
    log_path = tmp_path / "debug.log"
    monkeypatch.setenv("PERSONA_DEBUG_LOG_PATH", str(log_path))
    # PERSONA_MEMORY_DEBUG 未設定
    recall_log.log_keywords("x", [], ["k"])
    captured = capsys.readouterr()
    assert "recall.keywords" not in captured.err
    assert not log_path.exists()


def test_recall_emits_all_stages_when_debug_c(db, capsys, monkeypatch):
    """recall フル実行で keywords / hits / final の 3 段が出る。"""
    monkeypatch.setenv("PERSONA_MEMORY_DEBUG", "c")
    insert_new(db, FactCandidate("preference", "coffee", "深煎り", 6), [1.0] + [0.0] * 767)
    db.commit()

    client = FakeClient(
        keywords=["コーヒー"],
        embedding_map={"コーヒー": [1.0] + [0.0] * 767},
    )
    out = recall(db, "深煎り好き", client)
    assert out  # additionalContext あり

    err = capsys.readouterr().err
    assert "recall.keywords" in err
    assert "recall.hits" in err
    assert "recall.final" in err
    # additionalContext は stderr のログに含まれる (additionalContext 自体には汚染しない)
    assert "深煎り" in err
