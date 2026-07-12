import pytest
from fastapi.testclient import TestClient

from qqquestion.demo import build_demo_llm
from qqquestion.diff_analyzer import analyze
from qqquestion.knowledge_base import InMemoryKnowledgeBase
from qqquestion.server import AppDeps, create_app

from .conftest import SAMPLE_DIFF
from .test_session import CORRECT_ANSWERS


@pytest.fixture
def client(tmp_path):
    deps = AppDeps(
        llm=build_demo_llm(),
        kb=InMemoryKnowledgeBase(),
        data_dir=tmp_path,
        diff_provider=lambda repo: analyze(SAMPLE_DIFF if repo != "empty" else ""),
    )
    return TestClient(create_app(deps))


def _start(client) -> str:
    response = client.post("/quiz/start", json={"repo_path": "."})
    assert response.status_code == 200
    return response.json()["session_id"]


def test_health(client):
    assert client.get("/health").json()["status"] == "ok"


def test_start_requires_staged_diff(client):
    response = client.post("/quiz/start", json={"repo_path": "empty"})
    assert response.status_code == 400


def test_question_payload_has_no_answer(client):
    session_id = _start(client)
    body = client.get(f"/quiz/{session_id}/question").json()
    question = body["question"]
    assert question["number"] == 1
    serialized = str(body)
    assert "model_answer" not in serialized
    assert "rubric" not in serialized


def test_full_flow_via_api(client):
    session_id = _start(client)
    for number in range(1, 6):
        question = client.get(f"/quiz/{session_id}/question").json()["question"]
        assert question["number"] == number
        response = client.post(
            f"/quiz/{session_id}/answer",
            json={"answer": CORRECT_ANSWERS[question["id"]]},
        ).json()
        assert response["judgement"]["verdict"] == "correct"
        assert response["question_done"]
        assert response["model_answer"]  # 終わった問題は開示される

    assert client.get(f"/quiz/{session_id}/status").json()["status"] == "completed"
    report = client.get(f"/quiz/{session_id}/report").json()
    assert report["report"]["attempted"] == 5
    assert "セッション評価レポート" in report["rendered"]
    # 終了後の解答は 409
    assert (
        client.post(f"/quiz/{session_id}/answer", json={"answer": "x"}).status_code == 409
    )


def test_hint_and_giveup_via_api(client):
    session_id = _start(client)
    hint = client.post(f"/quiz/{session_id}/hint").json()["hint"]
    assert hint["hint"]
    response = client.post(f"/quiz/{session_id}/giveup").json()
    assert response["judgement"]["verdict"] == "incorrect"
    assert response["model_answer"]
    assert response["next_question"]["number"] == 2


def test_pending_claims_once(client):
    session_id = _start(client)
    first = client.get("/quiz/pending").json()["sessions"]
    assert [s["session_id"] for s in first] == [session_id]
    assert client.get("/quiz/pending").json()["sessions"] == []  # 二重表示しない


def test_abort_via_api(client):
    session_id = _start(client)
    assert client.post(f"/quiz/{session_id}/abort").json()["status"] == "aborted"
    assert client.get(f"/quiz/{session_id}/status").json()["status"] == "aborted"


def test_unknown_session_is_404(client):
    assert client.get("/quiz/nope/status").status_code == 404
