from qqquestion.judge import judge_answer
from qqquestion.models import Judgement


def test_exact_match_skips_llm(fake_llm, demo_questions):
    question = demo_questions[0]
    judgement = judge_answer(fake_llm, question, question.model_answer)
    assert judgement.verdict == "correct"
    assert judgement.reason == "許容解答と一致"
    assert fake_llm.calls == []  # LLM を呼ばない


def test_exact_match_normalizes_notation(fake_llm, demo_questions):
    question = demo_questions[0]
    # 全角・空白・句読点のゆれを吸収して一致させる
    noisy = "　" + question.model_answer.replace("、", " ") + "。"
    judgement = judge_answer(fake_llm, question, noisy)
    assert judgement.verdict == "correct"
    assert fake_llm.calls == []


def test_empty_answer_is_incorrect_without_llm(fake_llm, demo_questions):
    judgement = judge_answer(fake_llm, demo_questions[0], "   ")
    assert judgement.verdict == "incorrect"
    assert fake_llm.calls == []


def test_llm_judgement_used_for_free_text(fake_llm, demo_questions):
    fake_llm.enqueue(
        Judgement(verdict="partial", missing_points=["再帰結合"], reason="要点が不足")
    )
    judgement = judge_answer(fake_llm, demo_questions[0], "前の状態を使うから")
    assert judgement.verdict == "partial"
    assert fake_llm.calls[0]["temperature"] == 0.0  # 判定は temperature 0.0


def test_empty_reason_triggers_rejudge(fake_llm, demo_questions):
    fake_llm.enqueue(Judgement(verdict="correct", reason=""))
    fake_llm.enqueue(Judgement(verdict="correct", reason="要点をすべて満たす"))
    judgement = judge_answer(fake_llm, demo_questions[0], "自由記述の解答")
    assert judgement.reason == "要点をすべて満たす"
    assert len(fake_llm.calls) == 2


def test_demo_rule_judge(demo_llm, demo_questions):
    question = demo_questions[3]  # reversed の問題
    good = judge_answer(
        demo_llm, question, "deltaのt+1への依存があるので未来から過去の順で計算する"
    )
    assert good.verdict == "correct"
    bad = judge_answer(demo_llm, question, "そういう決まりだから")
    assert bad.verdict == "incorrect"
