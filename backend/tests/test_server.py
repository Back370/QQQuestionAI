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
        run_in_background=lambda task: task(),  # テストでは決定的に同期実行
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


def test_incorrect_answer_hides_grading_points(client):
    """未完了(不正解)の判定は accepted_points 由来の要点を漏らさない。

    matched_points / missing_points は正解の骨子そのものなので、問題が
    終わるまではクライアントへ返さない（answer/stream の途中経過抑止と同方針）。
    """
    session_id = _start(client)
    response = client.post(
        f"/quiz/{session_id}/answer", json={"answer": "全く関係のない答え"}
    ).json()
    assert response["judgement"]["verdict"] == "incorrect"
    assert response["question_done"] is False
    assert response.get("model_answer") is None
    assert response["judgement"]["missing_points"] == []
    assert response["judgement"]["matched_points"] == []
    assert response["judgement"]["reason"] == ""


def test_partial_answer_hides_points_but_keeps_feedback(client):
    """部分正解でも要点リストは伏せる。

    q1 の設問文自体が「再帰結合」を含む（設問がその語の使用を要求している）
    ため、漏洩チェックは設問文ではなく judgement オブジェクトに限定する。
    """
    session_id = _start(client)
    response = client.post(
        f"/quiz/{session_id}/answer",
        json={"answer": "隠れ層が再帰結合を持つ点が違います"},
    ).json()
    assert response["judgement"]["verdict"] == "partial"
    assert response["question_done"] is False
    assert response["judgement"]["missing_points"] == []
    assert response["judgement"]["matched_points"] == []
    accepted_points = ["再帰結合", "前の時刻の隠れ状態", "系列・文脈の保持"]
    judgement_serialized = str(response["judgement"])
    assert not any(point in judgement_serialized for point in accepted_points)


def test_hint_and_giveup_via_api(client):
    session_id = _start(client)
    hint = client.post(f"/quiz/{session_id}/hint").json()["hint"]
    assert hint["hint"]
    response = client.post(f"/quiz/{session_id}/giveup").json()
    assert response["judgement"]["verdict"] == "incorrect"
    assert response["model_answer"]
    assert response["next_question"]["number"] == 2


def test_pending_is_read_only_until_claimed(client):
    """GET /quiz/pending は副作用なし。claim するまで何度でも同じものを返す。

    旧実装は GET で claim していたため、claim 後に応答を落とすとセッションが
    誰にも配られず、タイムアウトの無いフックが無限待機してコミットが固まった。
    読み取り専用にしたことで、応答を取りこぼしても次の周回で再掲される。
    """
    session_id = _start(client)
    first = client.get("/quiz/pending").json()["sessions"]
    assert [s["session_id"] for s in first] == [session_id]
    # もう一度読んでも消えない（応答取りこぼしからの回復に必要）
    again = client.get("/quiz/pending").json()["sessions"]
    assert [s["session_id"] for s in again] == [session_id]


def test_claim_removes_from_pending_and_is_first_wins(client):
    session_id = _start(client)

    won = client.post(f"/quiz/{session_id}/claim").json()
    assert won == {"ok": True}
    # claim 後は一覧から消える（このパネルが所有した）
    assert client.get("/quiz/pending").json()["sessions"] == []
    # 二人目の claim は先着に負ける（別ウィンドウは abort せず閉じるための合図）
    assert client.post(f"/quiz/{session_id}/claim").json() == {"ok": False}


def test_claim_unknown_session_is_404(client):
    assert client.post("/quiz/nope/claim").status_code == 404


def _start_in(client, repo_path: str) -> str:
    response = client.post("/quiz/start", json={"repo_path": repo_path})
    assert response.status_code == 200
    return response.json()["session_id"]


def test_pending_routes_to_matching_workspace(client, tmp_path):
    """コミットが走ったリポジトリのワークスペースにだけクイズが出る。"""
    repo = tmp_path / "repoA"
    repo.mkdir()
    other = tmp_path / "repoB"
    other.mkdir()
    session_id = _start_in(client, str(repo))

    # 別リポジトリを開いたウィンドウ（repoB）は拾わない
    assert (
        client.get("/quiz/pending", params={"workspace": str(other)}).json()["sessions"]
        == []
    )
    # コミット元リポジトリを開いたウィンドウ（repoA）が拾う
    claimed = client.get("/quiz/pending", params={"workspace": str(repo)}).json()[
        "sessions"
    ]
    assert [s["session_id"] for s in claimed] == [session_id]


def test_pending_matches_subdirectory_commit(client, tmp_path):
    """サブディレクトリで commit しても、リポジトリを開いたウィンドウが拾う。"""
    repo = tmp_path / "repo"
    subdir = repo / "pkg"
    subdir.mkdir(parents=True)
    session_id = _start_in(client, str(subdir))  # pwd がサブディレクトリ

    claimed = client.get("/quiz/pending", params={"workspace": str(repo)}).json()[
        "sessions"
    ]
    assert [s["session_id"] for s in claimed] == [session_id]


def test_pending_grace_fallback_when_no_window_matches(client, tmp_path, monkeypatch):
    """どのウィンドウのワークスペースにも一致しなければ、猶予後に誰でも拾う。"""
    import qqquestion.server as server

    repo = tmp_path / "repo"
    repo.mkdir()
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    session_id = _start_in(client, str(repo))

    # 猶予内は担当外ウィンドウには出さない（取りこぼしを恐れて即出ししない）
    assert (
        client.get("/quiz/pending", params={"workspace": str(unrelated)}).json()[
            "sessions"
        ]
        == []
    )

    # 猶予を過ぎたら保険としてどのウィンドウでも拾える
    monkeypatch.setattr(server, "PENDING_GRACE_SECONDS", -1.0)
    claimed = client.get("/quiz/pending", params={"workspace": str(unrelated)}).json()[
        "sessions"
    ]
    assert [s["session_id"] for s in claimed] == [session_id]


def _start_with_origin(client, repo_path: str, origin: str) -> str:
    response = client.post(
        "/quiz/start", json={"repo_path": repo_path, "origin": origin}
    )
    assert response.status_code == 200
    return response.json()["session_id"]


def test_pending_skips_cli_origin(client, tmp_path):
    """端末の quiz にパネルを開かない（端末で解答中に横からパネルが出て、
    それを閉じると abort でセッションが死ぬのを防ぐ）。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    _start_with_origin(client, str(repo), "cli")

    assert (
        client.get("/quiz/pending", params={"workspace": str(repo)}).json()["sessions"]
        == []
    )


def test_pending_skips_ui_origin(client, tmp_path):
    """拡張のコマンド起点も、呼び出し元が自分でパネルを開くので載せない。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    _start_with_origin(client, str(repo), "ui")

    assert (
        client.get("/quiz/pending", params={"workspace": str(repo)}).json()["sessions"]
        == []
    )


def test_pending_grace_fallback_does_not_leak_non_hook_origin(
    client, tmp_path, monkeypatch
):
    """ワークスペース不一致時の保険（猶予後は誰でも拾う）が、cli 起点まで
    拾い直してしまわないこと。"""
    import qqquestion.server as server

    repo = tmp_path / "repo"
    repo.mkdir()
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    _start_with_origin(client, str(repo), "cli")

    monkeypatch.setattr(server, "PENDING_GRACE_SECONDS", -1.0)
    assert (
        client.get("/quiz/pending", params={"workspace": str(unrelated)}).json()[
            "sessions"
        ]
        == []
    )


def test_pending_defaults_to_hook_for_clients_without_origin(client, tmp_path):
    """origin を送らない旧クライアント（旧フック）は従来どおりパネルが開く。
    フックはパネルが開かないとコミット待ちのまま固まるため、既定は hook 側に倒す。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    session_id = _start_in(client, str(repo))  # origin 無しの POST

    claimed = client.get("/quiz/pending", params={"workspace": str(repo)}).json()[
        "sessions"
    ]
    assert [s["session_id"] for s in claimed] == [session_id]


def test_unknown_origin_is_rejected(client):
    response = client.post("/quiz/start", json={"repo_path": ".", "origin": "nope"})
    assert response.status_code == 422


def test_abort_via_api(client):
    session_id = _start(client)
    assert client.post(f"/quiz/{session_id}/abort").json()["status"] == "aborted"
    assert client.get(f"/quiz/{session_id}/status").json()["status"] == "aborted"


def test_unknown_session_is_404(client):
    assert client.get("/quiz/nope/status").status_code == 404
