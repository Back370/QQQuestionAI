"""出題の逐次確定とセッション即公開（パネル即時表示）のテスト。

- generate_questions_stream: 全問の生成完了を待たずに確定した問題から yield する
- QuizSession(defer_questions=True): 生成前にセッションを公開できる
- /quiz/start: 生成失敗時は fail-open（completed 扱いでコミットを通す）
"""

import pytest
from fastapi.testclient import TestClient

from qqquestion.demo import DEMO_QUESTIONS, build_demo_llm
from qqquestion.diff_analyzer import analyze
from qqquestion.knowledge_base import InMemoryKnowledgeBase
from qqquestion.models import QuestionSet
from qqquestion.question_gen import generate_questions_stream
from qqquestion.server import AppDeps, create_app
from qqquestion.session import QuizSession

from .conftest import SAMPLE_DIFF


def _demo_dict(index: int) -> dict:
    return DEMO_QUESTIONS[index].model_dump()


class ScriptedStreamLLM:
    """部分パース結果の列をそのまま流すテスト用 LLM。"""

    def __init__(self, partials: list[dict], final: QuestionSet, log: list[str]):
        self._partials = partials
        self._final = final
        self._log = log

    def generate(self, schema, system, user, temperature=0.0):
        return self._final

    def generate_stream(self, schema, system, user, temperature=0.0):
        for i, partial in enumerate(self._partials):
            self._log.append(f"partial{i}")
            yield ("partial", partial)
        self._log.append("final")
        yield ("final", self._final)


class ExplodingLLM:
    def generate(self, schema, system, user, temperature=0.0):
        raise RuntimeError("LLM が落ちました")


# ---- question_gen -----------------------------------------------------


def test_questions_publish_before_generation_completes(diff_ctx):
    log: list[str] = []
    llm = ScriptedStreamLLM(
        partials=[
            # q1, q2 が確定（最後の要素は生成途中扱い）
            {"questions": [_demo_dict(0), _demo_dict(1), {"id": "q3"}]},
            {"questions": [_demo_dict(i) for i in range(5)]},
        ],
        final=QuestionSet(questions=list(DEMO_QUESTIONS)),
        log=log,
    )
    gen = generate_questions_stream(llm, diff_ctx)

    first = next(gen)
    # 全問の生成完了（final）を待たずに第1問が確定している
    assert log == ["partial0"]
    assert first.id == "q1" and first.type == "prerequisite"

    second = next(gen)
    assert log == ["partial0"]  # 同じ部分結果から第2問も確定
    assert second.id == "q2"

    rest = list(gen)
    assert [q.id for q in rest] == ["q3", "q4", "q5"]
    assert [q.type for q in rest] == ["implementation"] * 3


def test_structure_violation_falls_back_to_batch(diff_ctx):
    # 先頭が implementation（構成違反）→ 先行確定せず、最終補正で 2+3 に直す
    shuffled = [DEMO_QUESTIONS[2], DEMO_QUESTIONS[0], DEMO_QUESTIONS[1],
                DEMO_QUESTIONS[3], DEMO_QUESTIONS[4]]
    log: list[str] = []
    llm = ScriptedStreamLLM(
        partials=[{"questions": [q.model_dump() for q in shuffled]}],
        final=QuestionSet(questions=shuffled),
        log=log,
    )
    questions = list(generate_questions_stream(llm, diff_ctx))
    assert [q.type for q in questions] == [
        "prerequisite", "prerequisite",
        "implementation", "implementation", "implementation",
    ]
    assert [q.id for q in questions] == ["q1", "q2", "q3", "q4", "q5"]


# ---- QuizSession の遅延生成 -------------------------------------------


def test_defer_questions_then_prepare(demo_llm, kb, diff_ctx):
    session = QuizSession(llm=demo_llm, kb=kb, diff_ctx=diff_ctx, defer_questions=True)
    # 生成前: パネルは開けるが問題はまだない
    assert session.preparing
    assert not session.finished
    assert session.total == 5  # 予定問数を先に見せる
    assert session.current_public() is None

    session.prepare()
    assert not session.preparing
    assert session.total == 5
    assert session.current_public()["number"] == 1


def test_prepare_fail_open_completes_session(kb, diff_ctx):
    session = QuizSession(
        llm=ExplodingLLM(), kb=kb, diff_ctx=diff_ctx, defer_questions=True
    )
    session.prepare(fail_open=True)
    assert session.status == "completed"  # 0問ならコミットを通す（従来のスキップ相当）
    assert session.error
    assert not session.preparing


def test_prepare_without_fail_open_raises(kb, diff_ctx):
    with pytest.raises(RuntimeError):
        QuizSession(llm=ExplodingLLM(), kb=kb, diff_ctx=diff_ctx)  # CLI 等の同期経路


# ---- サーバ -----------------------------------------------------------


def _deps(tmp_path, llm) -> AppDeps:
    return AppDeps(
        llm=llm,
        kb=InMemoryKnowledgeBase(),
        data_dir=tmp_path,
        diff_provider=lambda repo: analyze(SAMPLE_DIFF),
    )


def test_session_is_published_before_generation(tmp_path):
    demo = build_demo_llm()
    visible_at_generation: list[int] = []

    class SnoopLLM:
        def generate(self, schema, system, user, temperature=0.0):
            visible_at_generation.append(len(deps.sessions))
            return demo.generate(schema, system, user, temperature)

    deps = _deps(tmp_path, SnoopLLM())
    client = TestClient(create_app(deps))
    client.post("/quiz/start", json={"repo_path": "."})
    # 出題生成が走る時点でセッションは公開済み（= パネルはすぐ開ける）
    assert visible_at_generation and visible_at_generation[0] == 1


def test_start_fail_open_via_api(tmp_path):
    client = TestClient(create_app(_deps(tmp_path, ExplodingLLM())))
    body = client.post("/quiz/start", json={"repo_path": "."}).json()
    assert body["error"]
    session_id = body["session_id"]
    # フックは completed を見てコミットを続行できる
    assert client.get(f"/quiz/{session_id}/status").json()["status"] == "completed"
    question = client.get(f"/quiz/{session_id}/question").json()
    assert question["question"] is None
    assert question["error"]


def test_question_endpoint_reports_preparing_flag(tmp_path):
    client = TestClient(create_app(_deps(tmp_path, build_demo_llm())))
    session_id = client.post("/quiz/start", json={"repo_path": "."}).json()["session_id"]
    body = client.get(f"/quiz/{session_id}/question").json()
    assert body["preparing"] is False
    assert body["question"]["number"] == 1
    assert body["error"] is None
