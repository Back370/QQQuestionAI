"""ターミナル用 remote_cli（起動済みバックエンドへの HTTP クライアント）のテスト。

拡張利用者向けの `quiz` はこのモジュールを呼ぶ。API キーは VSCode の
SecretStorage にあってターミナルからは読めないため、キーを持つバックエンドに
実行を委譲する——という前提が壊れていないことを確認する。
"""

from __future__ import annotations

import io

import pytest

from qqquestion import remote_cli


class _FakeResponse:
    def __init__(self, status_code: int = 200, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise AssertionError(f"HTTP {self.status_code}")


def test_offline_backend_gives_actionable_message(capsys, monkeypatch) -> None:
    """バックエンド未起動なら、原因と対処が分かる案内を出して 1 で終わる。"""

    class _DeadClient:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, *args, **kwargs):
            raise ConnectionError("refused")

    monkeypatch.setattr(remote_cli, "_health", lambda client, port: False)
    monkeypatch.setitem(
        __import__("sys").modules, "httpx", type("M", (), {"Client": _DeadClient})
    )

    exit_code = remote_cli.run(repo=".", port=8756)
    assert exit_code == 1
    err = capsys.readouterr().err
    assert "バックエンドに接続できません" in err
    # 拡張利用者に「backend/ を用意しろ」と誤解させないこと
    assert "backend/" not in err
    assert "拡張" in err


def test_typewriter_prints_only_delta(capsys) -> None:
    """スナップショット方式のストリームで、増分だけを表示する。"""
    writer = remote_cli._TypeWriter()
    writer.update("こんに")
    writer.update("こんにちは")
    assert capsys.readouterr().out == "こんにちは"


def test_typewriter_falls_back_on_replacement(capsys) -> None:
    """全文が差し替わったら改行して出し直す（前置きが一致しない場合）。"""
    writer = remote_cli._TypeWriter()
    writer.update("最初の文")
    writer.update("別の文")
    assert capsys.readouterr().out == "最初の文\n別の文"


def test_iter_sse_parses_data_frames() -> None:
    class _Resp:
        def iter_text(self):
            yield 'data: {"event": "judgement_partial", "reason": "あ"}\n\n'
            yield 'data: {"event": "result", "status": "completed"}\n\n'

    events = list(remote_cli._iter_sse(_Resp()))
    assert [e["event"] for e in events] == ["judgement_partial", "result"]


def test_iter_sse_handles_split_frames() -> None:
    """チャンク境界がフレームを跨いでも取りこぼさない。"""

    class _Resp:
        def iter_text(self):
            yield 'data: {"event": "res'
            yield 'ult", "status": "completed"}\n\n'

    events = list(remote_cli._iter_sse(_Resp()))
    assert [e["event"] for e in events] == ["result"]


@pytest.mark.parametrize(
    "verdict,expected",
    [("correct", "正解です"), ("partial", "部分的に正解"), ("incorrect", "残念、違います")],
)
def test_print_verdict(capsys, verdict: str, expected: str) -> None:
    remote_cli._print_verdict(
        {"judgement": {"verdict": verdict, "reason": "理由"}, "question_done": False},
        streamed_reason=False,
    )
    assert expected in capsys.readouterr().out


def test_print_verdict_giveup_reveals_answer(capsys) -> None:
    remote_cli._print_verdict(
        {"judgement": {"verdict": "incorrect"}, "question_done": True, "model_answer": "答え"},
        streamed_reason=False,
    )
    assert "正解は「答え」でした" in capsys.readouterr().out


def test_module_holds_no_api_key_handling() -> None:
    """このクライアントは API キーを扱わない（バックエンドに委譲する）。"""
    source = (remote_cli.__file__ and open(remote_cli.__file__, encoding="utf-8").read()) or ""
    assert "GOOGLE_API_KEY" not in source
    assert "create_llm" not in source
