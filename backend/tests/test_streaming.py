"""半二重ストリーミング（逐次表示）のテスト。

LLM 層の generate_stream / stream_generate、セッションのイベント列、
SSE エンドポイントを FakeLLM で検証する（実APIは叩かない）。
"""

import json

import pytest
from fastapi.testclient import TestClient

from qqquestion import llm as llm_module
from qqquestion.demo import build_demo_llm
from qqquestion.diff_analyzer import analyze
from qqquestion.knowledge_base import InMemoryKnowledgeBase
from qqquestion.llm import GeminiLLM, LLMUnavailableError, stream_generate
from qqquestion.models import Judgement
from qqquestion.server import AppDeps, create_app
from qqquestion.session import AnswerResult, QuizSession

from .conftest import SAMPLE_DIFF
from .test_session import CORRECT_ANSWERS


# ---- LLM 層 -----------------------------------------------------------


def test_fake_llm_generate_stream_grows_then_finalizes(demo_llm):
    events = list(
        demo_llm.generate_stream(Judgement, "sys", "要点(accepted_points): []\n学習者の解答: x")
    )
    partials = [payload for name, payload in events if name == "partial"]
    finals = [payload for name, payload in events if name == "final"]
    assert len(finals) == 1
    assert isinstance(finals[0], Judgement)
    assert partials  # 途中経過がある
    assert partials[-1] == finals[0].model_dump()  # 最後の partial は最終形と一致
    # 文字列フィールドは徐々に伸びる（スナップショット単調増加）
    reasons = [p.get("reason", "") for p in partials if "reason" in p]
    assert all(
        later.startswith(earlier) for earlier, later in zip(reasons, reasons[1:])
    )


def test_stream_generate_falls_back_without_generate_stream():
    class OneShotLLM:
        def generate(self, schema, system, user, temperature=0.0):
            return Judgement(verdict="correct", reason="ok")

    events = list(stream_generate(OneShotLLM(), Judgement, "sys", "user"))
    assert [name for name, _ in events] == ["partial", "final"]
    assert events[1][1].reason == "ok"


# ---- GeminiLLM.generate_stream（issue #28）----------------------------
#
# ストリーミングだけが非ストリームより遅く、自分で設定した 45 秒の
# サーバ側デッドライン（X-Server-Timeout）に触れて 504 DEADLINE_EXCEEDED で
# 落ちていた。原因は「制約付きデコードを掛けず、スキーマをプロンプトに
# 貼って自力で守らせていた」こと。実APIは叩かず、渡す引数と失敗時の
# 振る舞いを固定する。


class _StubChat:
    """ChatGoogleGenerativeAI の代わり。stream() の入出力を記録する。"""

    def __init__(self, chunks=(), error=None):
        self._chunks = list(chunks)
        self._error = error
        self.seen_system = ""

    def stream(self, messages):
        self.seen_system = messages[0].content
        if self._error is not None:
            raise self._error
        for text in self._chunks:
            yield type("Chunk", (), {"content": text})()


def _stub_gemini(monkeypatch, chat, generate_result=None):
    """_chat と generate を差し替えた GeminiLLM と、記録用の入れ物を返す。"""
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    llm = GeminiLLM(model="gemini-3.5-flash")
    captured: dict = {}
    generate_calls: list = []

    def fake_chat(temperature, **extra):
        captured["temperature"] = temperature
        captured["extra"] = extra
        return chat

    def fake_generate(schema, system, user, temperature=0.0):
        generate_calls.append(schema)
        return generate_result

    monkeypatch.setattr(llm, "_chat", fake_chat)
    monkeypatch.setattr(llm, "generate", fake_generate)
    return llm, captured, generate_calls


def test_stream_uses_constrained_decoding_not_a_prompt_dump(monkeypatch):
    """非ストリームと同じ制約（response_schema）を API 側に渡す。

    プロンプトへスキーマ全文を貼る旧実装は、入力も出力も膨らませたうえ
    出力形式を保証しないため、ストリームだけがデッドラインに届いていた。
    """
    monkeypatch.setattr(llm_module, "_supports_response_schema", lambda: True)
    chat = _StubChat(['{"verdict": "correct",', ' "reason": "よい解答です"}'])
    llm, captured, generate_calls = _stub_gemini(monkeypatch, chat)

    events = list(llm.generate_stream(Judgement, "採点してください", "学習者の解答"))

    extra = captured["extra"]
    assert extra["response_mime_type"] == "application/json"
    assert set(extra["response_schema"]["properties"]) >= {"verdict", "reason"}
    # スキーマ本体はプロンプトに載せない（API 側の制約に任せる）
    assert '"properties"' not in chat.seen_system
    assert chat.seen_system.startswith("採点してください")
    assert not generate_calls  # 成功時は非ストリームを呼ばない
    assert [name for name, _ in events][-1] == "final"
    assert events[-1][1] == Judgement(verdict="correct", reason="よい解答です")


def test_stream_without_response_schema_support_keeps_prompt_schema(monkeypatch):
    """response_schema を持たない旧ライブラリでは従来どおりプロンプトで渡す。

    ChatGoogleGenerativeAI は extra="ignore" なので、渡した引数が黙って
    捨てられる。捨てられる版でプロンプトからもスキーマを外すと、
    出力形式の指示がどこにも無い状態に静かに退行する。
    """
    monkeypatch.setattr(llm_module, "_supports_response_schema", lambda: False)
    chat = _StubChat(['{"verdict": "partial", "reason": "あと少し"}'])
    llm, captured, _ = _stub_gemini(monkeypatch, chat)

    list(llm.generate_stream(Judgement, "採点してください", "学習者の解答"))

    assert "response_schema" not in captured["extra"]
    assert '"properties"' in chat.seen_system


def test_stream_deadline_does_not_double_wait(monkeypatch):
    """504 DEADLINE_EXCEEDED は非ストリームで作り直さず即座に伝える。

    作り直しても同じデッドラインに掛かるだけで、利用者の待ち時間が
    45秒 + 45秒 に伸びる（issue #28 のログはこの二重待ちの1周目）。
    """
    error = RuntimeError(
        "504 DEADLINE_EXCEEDED. Deadline expired before operation could complete."
    )
    chat = _StubChat(error=error)
    llm, _, generate_calls = _stub_gemini(monkeypatch, chat)

    with pytest.raises(LLMUnavailableError) as excinfo:
        list(llm.generate_stream(Judgement, "採点してください", "学習者の解答"))

    assert "タイムアウト" in str(excinfo.value)  # UI に出せる日本語になっている
    assert not generate_calls  # 二重待ちしない


def test_stream_falls_back_for_recoverable_errors(monkeypatch):
    """引数非対応・JSON 崩れなど、作り直せば直る失敗は非ストリームへ落とす。"""
    chat = _StubChat(error=TypeError("unexpected keyword argument 'response_schema'"))
    expected = Judgement(verdict="partial", reason="途中まで捉えられています")
    llm, _, generate_calls = _stub_gemini(monkeypatch, chat, generate_result=expected)

    events = list(llm.generate_stream(Judgement, "採点してください", "学習者の解答"))

    assert generate_calls == [Judgement]
    assert [name for name, _ in events] == ["partial", "final"]
    assert events[-1][1] == expected


# ---- セッション -------------------------------------------------------


@pytest.fixture
def session(demo_llm, kb, diff_ctx):
    return QuizSession(llm=demo_llm, kb=kb, diff_ctx=diff_ctx)


def test_submit_answer_stream_correct_event_order(session):
    question_id = session.current().question.id
    events = list(session.submit_answer_stream(CORRECT_ANSWERS[question_id]))
    names = [name for name, _ in events]

    assert names[-1] == "result"
    assert names.count("judgement") == 1
    assert "explanation_partial" in names
    # 判定確定は解説より前
    assert names.index("judgement") < names.index("explanation_partial")

    judgement_payload = dict(events[names.index("judgement")][1])
    assert judgement_payload["judgement"].verdict == "correct"
    assert judgement_payload["question_done"] is True
    assert judgement_payload["model_answer"]

    result = events[-1][1]
    assert isinstance(result, AnswerResult)
    assert result.question_done and result.explanation is not None
    assert session.current_public()["number"] == 2  # 次の問題へ進んでいる


def test_submit_answer_stream_incorrect_hides_reason_and_answer(session):
    events = list(session.submit_answer_stream("全く関係のない答え"))
    names = [name for name, _ in events]
    # 不正解: 判定理由の途中経過を流さない（欠けた要点＝答えの手がかりの漏洩防止）
    assert "judgement_partial" not in names
    assert "explanation_partial" not in names
    judgement_payload = dict(events[names.index("judgement")][1])
    assert judgement_payload["question_done"] is False
    assert judgement_payload["model_answer"] is None


def test_give_up_stream_reveals_answer_then_streams_explanation(session):
    model_answer = session.current().question.model_answer
    events = list(session.give_up_stream())
    names = [name for name, _ in events]
    assert names[0] == "judgement"
    assert dict(events[0][1])["model_answer"] == model_answer
    assert "explanation_partial" in names
    assert names[-1] == "result"


def test_stream_and_oneshot_give_same_result(demo_llm, kb, diff_ctx):
    streamed = QuizSession(llm=build_demo_llm(), kb=kb, diff_ctx=diff_ctx)
    oneshot = QuizSession(llm=build_demo_llm(), kb=kb, diff_ctx=diff_ctx)
    answer = CORRECT_ANSWERS[streamed.current().question.id]

    result_stream = [
        payload for name, payload in streamed.submit_answer_stream(answer)
        if name == "result"
    ][0]
    result_oneshot = oneshot.submit_answer(answer)
    assert result_stream.judgement == result_oneshot.judgement
    assert result_stream.model_answer == result_oneshot.model_answer


# ---- SSE エンドポイント -----------------------------------------------


@pytest.fixture
def client(tmp_path):
    deps = AppDeps(
        llm=build_demo_llm(),
        kb=InMemoryKnowledgeBase(),
        data_dir=tmp_path,
        diff_provider=lambda repo: analyze(SAMPLE_DIFF),
        run_in_background=lambda task: task(),  # テストでは決定的に同期実行
    )
    return TestClient(create_app(deps))


def _start(client) -> str:
    return client.post("/quiz/start", json={"repo_path": "."}).json()["session_id"]


def _sse_events(response) -> list[dict]:
    events = []
    for block in response.text.split("\n\n"):
        block = block.strip()
        if block.startswith("data: "):
            events.append(json.loads(block[len("data: "):]))
    return events


def test_answer_stream_endpoint(client):
    session_id = _start(client)
    question = client.get(f"/quiz/{session_id}/question").json()["question"]
    with client.stream(
        "POST",
        f"/quiz/{session_id}/answer/stream",
        json={"answer": CORRECT_ANSWERS[question["id"]]},
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        response.read()
        events = _sse_events(response)

    names = [event["event"] for event in events]
    assert names[-1] == "result"
    assert "judgement" in names and "explanation_partial" in names
    result = events[-1]
    # 非ストリーム版 /answer と同じフィールドを持つ
    assert result["judgement"]["verdict"] == "correct"
    assert result["question_done"] and result["model_answer"]
    assert result["next_question"]["number"] == 2
    assert result["status"] == "in_progress"


def test_answer_stream_does_not_leak_answer_before_done(client):
    session_id = _start(client)
    response = client.post(
        f"/quiz/{session_id}/answer/stream", json={"answer": "全く関係のない答え"}
    )
    events = _sse_events(response)
    names = [event["event"] for event in events]
    assert "judgement_partial" not in names  # 不正解の理由は途中経過を流さない
    assert "explanation_partial" not in names  # 解説は問題が終わるまで流れない
    for event in events:
        assert not event.get("model_answer")  # 模範解答は開示されない


def test_answer_stream_judgement_hides_grading_points_before_done(client):
    """SSE の judgement イベントも未完了時は要点(accepted_points)を伏せる。"""
    session_id = _start(client)
    response = client.post(
        f"/quiz/{session_id}/answer/stream", json={"answer": "全く関係のない答え"}
    )
    events = _sse_events(response)
    judgement_events = [e for e in events if e["event"] == "judgement"]
    assert judgement_events
    accepted_points = ["再帰結合", "前の時刻の隠れ状態", "系列・文脈の保持"]
    for event in judgement_events:
        assert event["question_done"] is False
        assert event["judgement"]["missing_points"] == []
        assert event["judgement"]["matched_points"] == []
        # judgement オブジェクト内に要点そのものが現れないこと（設問文は別）
        judgement_serialized = str(event["judgement"])
        assert not any(point in judgement_serialized for point in accepted_points)


def test_giveup_stream_endpoint(client):
    session_id = _start(client)
    response = client.post(f"/quiz/{session_id}/giveup/stream")
    events = _sse_events(response)
    names = [event["event"] for event in events]
    assert names[0] == "judgement"
    assert events[0]["question_done"] and events[0]["model_answer"]
    assert names[-1] == "result"
    assert events[-1]["next_question"]["number"] == 2


class _JudgementFailsLLM:
    """出題はできるが判定だけ API 不能で落ちる LLM（タイムアウト相当）。"""

    def __init__(self):
        self._inner = build_demo_llm()

    def generate(self, schema, system, user, temperature=0.0):
        if schema is Judgement:
            raise LLMUnavailableError(
                "AIサービスが時間内に応答しませんでした（タイムアウト）。"
            )
        return self._inner.generate(schema, system, user, temperature=temperature)


def test_answer_stream_reports_failure_as_error_event(tmp_path):
    """ストリーム途中の生成失敗は、接続を黙って切らず error イベントで伝える。

    SSE はヘッダを先に 200 で返すので、例外をそのまま投げると UI からは
    「何も起きない」ように見え、失敗の理由が届かなかった（issue #28）。
    """
    deps = AppDeps(
        llm=_JudgementFailsLLM(),
        kb=InMemoryKnowledgeBase(),
        data_dir=tmp_path,
        diff_provider=lambda repo: analyze(SAMPLE_DIFF),
        run_in_background=lambda task: task(),
    )
    client = TestClient(create_app(deps))
    session_id = _start(client)

    response = client.post(
        f"/quiz/{session_id}/answer/stream", json={"answer": "なにか答え"}
    )
    assert response.status_code == 200
    events = _sse_events(response)
    assert events[-1]["event"] == "error"
    assert "タイムアウト" in events[-1]["message"]


def test_answer_stream_finished_session_is_409(client):
    session_id = _start(client)
    for _ in range(5):
        client.post(f"/quiz/{session_id}/giveup")
    response = client.post(
        f"/quiz/{session_id}/answer/stream", json={"answer": "x"}
    )
    assert response.status_code == 409
