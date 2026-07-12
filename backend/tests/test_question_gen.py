import pytest

from qqquestion.models import Question, QuestionSet
from qqquestion.question_gen import generate_questions


def _make_question(question_id: str, question_type: str) -> Question:
    return Question(
        id=question_id,
        type=question_type,
        text=f"問題 {question_id}",
        model_answer="答え",
        accepted_points=["要点"],
        rubric="要点があれば正解",
        topic="RNN",
    )


def test_structure_is_enforced(fake_llm, diff_ctx):
    # LLM が implementation×4 + prerequisite×1 を返しても 2+3 に補正される
    fake_llm.enqueue(
        QuestionSet(
            questions=[
                _make_question("a", "implementation"),
                _make_question("b", "implementation"),
                _make_question("c", "implementation"),
                _make_question("d", "implementation"),
                _make_question("e", "prerequisite"),
            ]
        )
    )
    questions = generate_questions(fake_llm, diff_ctx)
    assert [q.id for q in questions] == ["q1", "q2", "q3", "q4", "q5"]
    assert [q.type for q in questions] == [
        "prerequisite", "prerequisite", "implementation", "implementation", "implementation",
    ]


def test_too_few_questions_raises(fake_llm, diff_ctx):
    fake_llm.enqueue(QuestionSet(questions=[_make_question("a", "prerequisite")]))
    with pytest.raises(ValueError):
        generate_questions(fake_llm, diff_ctx)


def test_weak_topics_and_difficulty_in_prompt(fake_llm, diff_ctx):
    fake_llm.enqueue(
        QuestionSet(
            questions=[
                _make_question("a", "prerequisite"),
                _make_question("b", "prerequisite"),
                _make_question("c", "implementation"),
                _make_question("d", "implementation"),
                _make_question("e", "implementation"),
            ]
        )
    )
    generate_questions(
        fake_llm, diff_ctx, weak_topics=["勾配計算"], difficulty_bias={"RNN": 2}
    )
    prompt = fake_llm.calls[0]["user"]
    assert "苦手トピック" in prompt and "勾配計算" in prompt
    assert "RNN=2" in prompt
    assert "```diff" in prompt


def test_demo_llm_returns_five_rnn_questions(demo_llm, diff_ctx):
    questions = generate_questions(demo_llm, diff_ctx)
    assert len(questions) == 5
    assert all(q.model_answer for q in questions)
    assert all(q.rubric for q in questions)
