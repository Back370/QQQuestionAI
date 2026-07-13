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


def test_already_matched_points_carry_over(fake_llm, demo_questions):
    """前回満たした要点は再度言及しなくても正解になる。"""
    question = demo_questions[0]  # 要点3つ
    # 今回の解答は残り2要点だけをカバー（「再帰結合」には触れていない）
    fake_llm.enqueue(
        Judgement(
            verdict="partial",
            matched_points=["前の時刻の隠れ状態", "系列・文脈の保持"],
            missing_points=["再帰結合"],
            reason="残りの要点を満たしました",
        )
    )
    judgement = judge_answer(
        fake_llm, question, "前の時刻の状態で文脈を保持する", already_matched=["再帰結合"]
    )
    assert judgement.verdict == "correct"  # 合算で全要点 → 正解
    assert set(judgement.matched_points) == set(question.accepted_points)
    assert "前回までの解答と合わせて" in judgement.reason
    # プロンプトに「既に満たした要点」が伝わっている
    assert "再度の言及を要求しないこと" in fake_llm.calls[0]["user"]


def test_partial_accumulates_but_stays_partial(fake_llm, demo_questions):
    question = demo_questions[0]
    fake_llm.enqueue(
        Judgement(
            verdict="partial",
            matched_points=["前の時刻の隠れ状態"],
            missing_points=["系列・文脈の保持"],
            reason="まだ足りません",
        )
    )
    judgement = judge_answer(
        fake_llm, question, "前の時刻の状態を使う", already_matched=["再帰結合"]
    )
    assert judgement.verdict == "partial"
    assert set(judgement.matched_points) == {"再帰結合", "前の時刻の隠れ状態"}
    assert judgement.missing_points == ["系列・文脈の保持"]


def test_llm_paraphrased_points_are_canonicalized(fake_llm, demo_questions):
    """LLM が要点を言い換えて返しても accepted_points に対応付けて数える。"""
    question = demo_questions[0]
    fake_llm.enqueue(
        Judgement(
            verdict="partial",
            matched_points=["再帰結合について"],  # 完全一致ではない
            missing_points=[],
            reason="一部を満たす",
        )
    )
    judgement = judge_answer(fake_llm, question, "再帰結合がある")
    assert judgement.matched_points == ["再帰結合"]  # 正規の表記に揃う
