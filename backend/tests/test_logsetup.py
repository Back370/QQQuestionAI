"""ファイルログ設定のテスト。

生成失敗の原因（生の例外・トレースバック）が server.log に残ることを確認する。
拡張の出力チャンネルは消えてしまうため、ログファイルが唯一の恒久的な手掛かり。
"""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

from qqquestion.knowledge_base import InMemoryKnowledgeBase
from qqquestion.logsetup import LOG_FILE_NAME, setup_file_logging
from qqquestion.session import QuizSession

from .test_preparing import ExplodingLLM


@pytest.fixture
def file_logging(tmp_path):
    """一時ディレクトリへのファイルログを設定し、テスト後にハンドラを外す。"""
    log_path = setup_file_logging(tmp_path)
    yield log_path
    root = logging.getLogger()
    for handler in list(root.handlers):
        if isinstance(handler, RotatingFileHandler) and Path(
            handler.baseFilename
        ) == log_path:
            root.removeHandler(handler)
            handler.close()


def test_setup_creates_log_file(file_logging, tmp_path):
    assert file_logging == (tmp_path / LOG_FILE_NAME).resolve()
    logging.getLogger("qqquestion.test").info("ログ出力テスト")
    assert "ログ出力テスト" in file_logging.read_text(encoding="utf-8")


def test_setup_is_idempotent(file_logging, tmp_path):
    setup_file_logging(tmp_path)  # 2回目
    root = logging.getLogger()
    same_file = [
        h
        for h in root.handlers
        if isinstance(h, RotatingFileHandler)
        and Path(h.baseFilename) == file_logging
    ]
    assert len(same_file) == 1

    logging.getLogger("qqquestion.test").info("一度だけ")
    content = file_logging.read_text(encoding="utf-8")
    assert content.count("一度だけ") == 1


def test_generation_failure_is_logged_with_traceback(file_logging, diff_ctx):
    """fail-open で握りつぶされる生成失敗でも、生の例外がログに残る。"""
    session = QuizSession(
        llm=ExplodingLLM(),
        kb=InMemoryKnowledgeBase(),
        diff_ctx=diff_ctx,
        defer_questions=True,
    )
    session.prepare_first(fail_open=True)

    assert session.status == "completed"  # fail-open でコミットは通る
    content = file_logging.read_text(encoding="utf-8")
    assert "第1問の生成に失敗しました" in content
    assert session.id in content
    assert "LLM が落ちました" in content  # 生の例外メッセージ
    assert "Traceback" in content  # トレースバック付き
