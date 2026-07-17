"""API が利用できないときのハング対策とフィードバックのテスト。

- タイムアウト/レート制限/認証失敗を、UI に出せる日本語メッセージに変換する
- APIキー未設定は起動時ではなく呼び出し時に LLMUnavailableError にする
  （サーバは立ち上がったまま、fail-open 経路でユーザーに理由を伝えるため）
- prepare_first が LLMUnavailableError でも fail-open でコミットを通し、
  理由を session.error に載せる（拡張が「生成に失敗」として表示できる）
- クイズ中（判定・ヒント・解説）にキーが失効しても固まらない: セッションを
  畳んで理由を伝え、フックがコミットを続行できる状態にする
"""

import pytest
from fastapi.testclient import TestClient

from qqquestion.demo import build_demo_llm
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
from .test_streaming import _sse_events


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


# ---- クイズ中にキーが失効しても固まらない ----------------------------
#
# 出題は通ったのに、その後の判定・ヒント・解説で API が死ぬ経路（キーの
# 利用期限切れが典型）。ここを素通しにすると、SSE は 200 のまま接続が切れて
# UI が result を待ち続け、セッションも in_progress のまま残ってフックが
# コミットを待ち続ける（＝固まる）。


AUTH_ERROR = "APIキーが無効か権限がありません。GOOGLE_API_KEY を確認してください。"
# 判定で LLM を通す（許容解答と一致すると LLM を呼ばずに正解になる）ための解答
NO_MATCH_ANSWER = "全く関係のない答え"


class ExpiringLLM:
    """途中で API キーが失効する LLM。出題は成功し、以降の呼び出しが失敗する。"""

    def __init__(self):
        self._inner = build_demo_llm()
        self.expired = False

    def _check(self) -> None:
        if self.expired:
            raise LLMUnavailableError(AUTH_ERROR)

    def generate(self, schema, system, user, temperature=0.0):
        self._check()
        return self._inner.generate(schema, system, user, temperature=temperature)

    def generate_stream(self, schema, system, user, temperature=0.0):
        self._check()
        yield from self._inner.generate_stream(
            schema, system, user, temperature=temperature
        )


def test_answer_after_key_expiry_ends_session(kb, diff_ctx):
    """判定中に失効 → セッションを畳んで理由を伝える（例外は握りつぶさない）。"""
    llm = ExpiringLLM()
    session = QuizSession(llm=llm, kb=kb, diff_ctx=diff_ctx)
    llm.expired = True

    with pytest.raises(LLMUnavailableError):
        session.submit_answer(NO_MATCH_ANSWER)

    # aborted ではなく completed: クイズはコミットを妨げない（fail-open）
    assert session.status == "completed"
    assert "APIキー" in (session.error or "")
    # ただし完走ではないので、レポートは「中断」として報告する
    assert not session.report().completed
    assert "(中断)" in session.report().render()


def test_hint_after_key_expiry_ends_session(kb, diff_ctx):
    llm = ExpiringLLM()
    session = QuizSession(llm=llm, kb=kb, diff_ctx=diff_ctx)
    llm.expired = True

    with pytest.raises(LLMUnavailableError):
        session.request_hint()
    assert session.status == "completed"
    assert "APIキー" in (session.error or "")


def test_explanation_failure_after_giveup_ends_session(kb, diff_ctx):
    """ギブアップ後の解説で失効 → 模範解答まで出してから畳む。"""
    llm = ExpiringLLM()
    session = QuizSession(llm=llm, kb=kb, diff_ctx=diff_ctx)
    llm.expired = True

    names = []
    with pytest.raises(LLMUnavailableError):
        for name, _payload in session.give_up_stream():
            names.append(name)

    assert names == ["judgement"]  # 模範解答は開示済み。解説の手前で落ちた
    assert session.status == "completed"


@pytest.fixture
def expiring(tmp_path) -> tuple[TestClient, ExpiringLLM]:
    llm = ExpiringLLM()
    deps = AppDeps(
        llm=llm,
        kb=InMemoryKnowledgeBase(),
        data_dir=tmp_path,
        diff_provider=lambda repo: analyze(SAMPLE_DIFF),
        run_in_background=lambda task: task(),
    )
    return TestClient(create_app(deps)), llm


def _start(client: TestClient) -> str:
    return client.post("/quiz/start", json={"repo_path": "."}).json()["session_id"]


def test_answer_stream_ends_with_error_event(expiring):
    """SSE は必ず終端イベントで終わる（UI が result を待ち続けない）。"""
    client, llm = expiring
    session_id = _start(client)
    llm.expired = True

    response = client.post(
        f"/quiz/{session_id}/answer/stream", json={"answer": NO_MATCH_ANSWER}
    )
    assert response.status_code == 200
    events = _sse_events(response)
    assert events[-1]["event"] == "error"
    assert "APIキー" in events[-1]["message"]
    assert events[-1]["status"] == "completed"
    # フックは status を見てコミットを続行できる（待ち続けない）
    assert client.get(f"/quiz/{session_id}/status").json()["status"] == "completed"


def test_giveup_stream_ends_with_error_event(expiring):
    client, llm = expiring
    session_id = _start(client)
    llm.expired = True

    events = _sse_events(client.post(f"/quiz/{session_id}/giveup/stream"))
    assert [event["event"] for event in events] == ["judgement", "error"]
    assert "APIキー" in events[-1]["message"]


def test_hint_returns_503_with_reason(expiring):
    """500 + 生スタックではなく、利用者向けの理由付き 503 を返す。"""
    client, llm = expiring
    session_id = _start(client)
    llm.expired = True

    response = client.post(f"/quiz/{session_id}/hint")
    assert response.status_code == 503
    assert "APIキー" in response.json()["detail"]
    assert client.get(f"/quiz/{session_id}/status").json()["status"] == "completed"


def test_answer_returns_503_with_reason(expiring):
    client, llm = expiring
    session_id = _start(client)
    llm.expired = True

    response = client.post(
        f"/quiz/{session_id}/answer", json={"answer": NO_MATCH_ANSWER}
    )
    assert response.status_code == 503
    assert "APIキー" in response.json()["detail"]


def test_report_after_key_expiry_is_not_a_completion(expiring):
    """途中で畳んだセッションを「完走」と報告しない（フック・拡張の誤表示防止）。"""
    client, llm = expiring
    session_id = _start(client)
    llm.expired = True
    client.post(f"/quiz/{session_id}/answer/stream", json={"answer": NO_MATCH_ANSWER})

    body = client.get(f"/quiz/{session_id}/report").json()
    assert body["report"]["completed"] is False
    # フックはこの error を「スキップの理由」として表示する
    assert "APIキー" in body["error"]
