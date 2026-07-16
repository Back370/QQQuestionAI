"""API が利用できないときのハング対策とフィードバックのテスト。

- タイムアウト/レート制限/認証失敗を、UI に出せる日本語メッセージに変換する
- APIキー未設定は起動時ではなく呼び出し時に LLMUnavailableError にする
  （サーバは立ち上がったまま、fail-open 経路でユーザーに理由を伝えるため）
- prepare_first が LLMUnavailableError でも fail-open でコミットを通し、
  理由を session.error に載せる（拡張が「生成に失敗」として表示できる）
"""

import pytest
from fastapi.testclient import TestClient

from qqquestion.diff_analyzer import analyze
from qqquestion.knowledge_base import InMemoryKnowledgeBase
from qqquestion.llm import (
    DEFAULT_MODEL,
    DEFAULT_TIMEOUT,
    GeminiLLM,
    LLMUnavailableError,
    _classify_llm_error,
    _fast_thinking_kwargs,
    _is_unavailable_error,
    _llm_timeout,
)
from qqquestion.server import AppDeps, create_app
from qqquestion.session import QuizSession

from .conftest import SAMPLE_DIFF


# ---- エラー分類 -------------------------------------------------------


def test_classify_timeout():
    assert "タイムアウト" in _classify_llm_error(TimeoutError("timed out"))
    assert "タイムアウト" in _classify_llm_error(RuntimeError("504 Deadline exceeded"))


def test_classify_rate_limit():
    assert "利用上限" in _classify_llm_error(RuntimeError("429 quota exceeded"))
    assert "利用上限" in _classify_llm_error(RuntimeError("Resource has been exhausted"))


def test_classify_auth():
    assert "APIキー" in _classify_llm_error(RuntimeError("API key not valid (401)"))
    assert "APIキー" in _classify_llm_error(RuntimeError("PermissionDenied"))


def test_classify_unknown_keeps_detail():
    message = _classify_llm_error(RuntimeError("なにか未知の失敗"))
    assert "なにか未知の失敗" in message


def test_is_unavailable_error_distinguishes_fallbackable():
    # フォールバックしても直らない失敗
    assert _is_unavailable_error(TimeoutError())
    assert _is_unavailable_error(RuntimeError("429 quota"))
    # thinking パラメータ非対応など、通常経路で作り直せる失敗
    assert not _is_unavailable_error(TypeError("unexpected keyword 'thinking_budget'"))


# ---- モデル提供終了(404) ---------------------------------------------


def test_classify_retired_model_guides_to_qqq_model(monkeypatch):
    """退役モデルの 404 は、生の例外ではなく次の一手を案内する。

    Google は退役モデルを新規プロジェクトにだけ 404 にするため、既存キーの
    開発者では再現せず、新しくAPIキーを取った利用者だけが踏む。
    """
    monkeypatch.setenv("QQQ_MODEL", "gemini-2.5-flash")
    error = RuntimeError(
        "Error calling model 'gemini-2.5-flash' (NOT_FOUND): 404 NOT_FOUND. "
        "This model is no longer available to new users."
    )
    message = _classify_llm_error(error)
    assert "gemini-2.5-flash" in message  # どのモデルが駄目なのか
    assert "QQQ_MODEL" in message  # どう直すのか


def test_retired_model_404_does_not_double_wait():
    """404 は通常生成へ落としても同じ 404 になるので即座に伝える（二重待ち回避）。"""
    error = RuntimeError("404 NOT_FOUND: no longer available to new users")
    assert _is_unavailable_error(error)


# ---- 速度優先の thinking パラメータはモデル世代で変わる ---------------


def test_fast_thinking_kwargs_per_model_generation():
    # Gemini 3 以降は thinking_budget を受け付けず thinking_level を使う。
    # 完全な無効化は不可なので最小の "minimal"。
    assert _fast_thinking_kwargs("gemini-3.5-flash") == {"thinking_level": "minimal"}
    assert _fast_thinking_kwargs("gemini-3-flash") == {"thinking_level": "minimal"}
    # 2.x 系は従来どおり thinking_budget=0 で無効化できる
    assert _fast_thinking_kwargs("gemini-2.5-flash") == {"thinking_budget": 0}
    # 未知の名前は従来動作（非対応なら generate へフォールバックする）
    assert _fast_thinking_kwargs("some-custom-model") == {"thinking_budget": 0}


def test_fast_thinking_kwargs_matches_installed_library():
    """渡す引数名が langchain-google-genai に実在することを固定する。

    存在しない引数名だと pydantic の extra=ignore で黙って捨てられ、
    thinking が既定(medium)のまま「速度優先のはずが遅い」に静かに退行する。
    """
    genai = pytest.importorskip("langchain_google_genai")
    fields = genai.ChatGoogleGenerativeAI.model_fields
    for model in ("gemini-3.5-flash", "gemini-2.5-flash"):
        for name in _fast_thinking_kwargs(model):
            assert name in fields, f"{name} が langchain-google-genai に無い"


def test_default_model_is_not_a_retired_one():
    """既定モデルは新規プロジェクトで 404 になったものに戻さない。"""
    assert DEFAULT_MODEL not in {"gemini-2.0-flash", "gemini-2.5-flash"}


# ---- タイムアウト設定 -------------------------------------------------


def test_llm_timeout_default_and_override(monkeypatch):
    monkeypatch.delenv("QQQ_LLM_TIMEOUT", raising=False)
    assert _llm_timeout() == DEFAULT_TIMEOUT
    monkeypatch.setenv("QQQ_LLM_TIMEOUT", "5")
    assert _llm_timeout() == 5.0
    monkeypatch.setenv("QQQ_LLM_TIMEOUT", "not-a-number")
    assert _llm_timeout() == DEFAULT_TIMEOUT  # 不正値は既定へ


# ---- APIキー未設定は呼び出し時に伝える -------------------------------


def test_missing_key_defers_to_call_time(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    llm = GeminiLLM()  # 起動時（construct）には失敗しない
    with pytest.raises(LLMUnavailableError) as excinfo:
        llm.generate_fast(object, "system", "user")  # 呼び出しで初めて失敗
    assert "GOOGLE_API_KEY" in str(excinfo.value)


# ---- fail-open でユーザーに理由が届く ---------------------------------


class UnavailableLLM:
    """毎回 API 不能で失敗する LLM（ハング→タイムアウト相当）。"""

    def generate(self, schema, system, user, temperature=0.0):
        raise LLMUnavailableError(
            "AIサービスが時間内に応答しませんでした（タイムアウト）。"
        )


def test_prepare_first_surfaces_unavailable_reason(kb, diff_ctx):
    session = QuizSession(llm=UnavailableLLM(), kb=kb, diff_ctx=diff_ctx,
                          defer_questions=True)
    session.prepare_first(fail_open=True)
    assert session.status == "completed"  # コミットは通す
    assert "タイムアウト" in (session.error or "")  # 理由がUIに届く
    assert not session.preparing


def test_start_endpoint_reports_unavailable_reason(tmp_path):
    deps = AppDeps(
        llm=UnavailableLLM(),
        kb=InMemoryKnowledgeBase(),
        data_dir=tmp_path,
        diff_provider=lambda repo: analyze(SAMPLE_DIFF),
        run_in_background=lambda task: task(),
    )
    client = TestClient(create_app(deps))
    body = client.post("/quiz/start", json={"repo_path": "."}).json()
    assert "タイムアウト" in body["error"]
    session_id = body["session_id"]
    # フックは completed を見てコミットを続行できる
    assert client.get(f"/quiz/{session_id}/status").json()["status"] == "completed"
    # 拡張はこの error を出して「スキップ」を表示する
    question = client.get(f"/quiz/{session_id}/question").json()
    assert question["question"] is None
    assert "タイムアウト" in question["error"]
