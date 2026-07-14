"""半二重ストリーミング（逐次表示）のテスト。

LLM 層の generate_stream / stream_generate、セッションのイベント列、
SSE エンドポイントを FakeLLM で検証する（実APIは叩かない）。
"""

import json

import pytest
from fastapi.testclient import TestClient

from qqquestion.demo import build_demo_llm
from qqquestion.diff_analyzer import analyze
from qqquestion.knowledge_base import InMemoryKnowledgeBase
from qqquestion.llm import stream_generate
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


def test_answer_stream_finished_session_is_409(client):
    session_id = _start(client)
    for _ in range(5):
        client.post(f"/quiz/{session_id}/giveup")
    response = client.post(
        f"/quiz/{session_id}/answer/stream", json={"answer": "x"}
    )
    assert response.status_code == 409
