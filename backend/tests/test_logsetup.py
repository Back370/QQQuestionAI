"""ファイルログ設定のテスト。

生成失敗の原因（生の例外・トレースバック）が server.log に残ることを確認する。
拡張の出力チャンネルは消えてしまうため、ログファイルが唯一の恒久的な手掛かり。
"""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

from qqquestion.knowledge_base import InMemoryKnowledgeBase
from qqquestion.logsetup import (
    LOG_FILE_NAME,
    _DropSuccessfulPollingAccessLogs,
    setup_file_logging,
)
from qqquestion.session import QuizSession

from .test_preparing import ExplodingLLM


@pytest.fixture
def file_logging(tmp_path):
    """一時ディレクトリへのファイルログを設定し、テスト後にハンドラを外す。"""
    log_path = setup_file_logging(tmp_path)
    yield log_path
    _teardown_logging(log_path)


def _teardown_logging(log_path: Path) -> None:
    """ハンドラとフィルタを外す。どちらもロガーに残ると他テストに漏れる。"""
    root = logging.getLogger()
    for handler in list(root.handlers):
        if isinstance(handler, RotatingFileHandler) and Path(
            handler.baseFilename
        ) == log_path:
            root.removeHandler(handler)
            handler.close()
    access = logging.getLogger("uvicorn.access")
    for f in list(access.filters):
        if isinstance(f, _DropSuccessfulPollingAccessLogs):
            access.removeFilter(f)


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


def _access_log(path: str, status: int, method: str = "GET") -> None:
    """uvicorn のアクセスログと同じ形（args 5要素）で1件出す。"""
    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d', "127.0.0.1:50752", method, path, "1.1", status
    )


def test_polling_success_access_logs_are_dropped(file_logging):
    """ポーリングの 200 はログを埋めるだけなので残さない。"""
    _access_log("/health", 200)
    _access_log("/quiz/pending?workspace=%2Ftmp%2Frepo", 200)

    content = file_logging.read_text(encoding="utf-8")
    assert "/health" not in content
    assert "/quiz/pending" not in content


def test_polling_errors_are_kept(file_logging):
    """エラー応答は原因調査に必要なので、ポーリング先でも必ず残す。"""
    _access_log("/quiz/pending?workspace=%2Ftmp%2Frepo", 500)
    _access_log("/health", 503)

    content = file_logging.read_text(encoding="utf-8")
    assert "/quiz/pending" in content
    assert "/health" in content


def test_other_endpoints_are_kept(file_logging):
    """クイズ本体のアクセスログは落とさない（POST や他パスは対象外）。"""
    _access_log("/quiz/start", 200, method="POST")
    _access_log("/quiz/abc123/question", 200)

    content = file_logging.read_text(encoding="utf-8")
    assert "/quiz/start" in content
    assert "/quiz/abc123/question" in content


def test_non_access_records_are_kept(file_logging):
    """args の形が違うレコード（通常のログ）を巻き添えにしない。"""
    logging.getLogger("uvicorn.access").info("何かの自由形式メッセージ /health")
    assert "何かの自由形式メッセージ" in file_logging.read_text(encoding="utf-8")


def test_polling_logs_can_be_restored_by_env(tmp_path, monkeypatch):
    """QQQ_LOG_POLLING=1 ならポーリングも残す（ポーリング自体の調査用）。"""
    monkeypatch.setenv("QQQ_LOG_POLLING", "1")
    log_path = setup_file_logging(tmp_path)
    try:
        _access_log("/health", 200)
        assert "/health" in log_path.read_text(encoding="utf-8")
    finally:
        _teardown_logging(log_path)


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
