import json

from qqquestion.evaluator import build_report, evaluate_judge
from qqquestion.models import Interaction, Judgement


def _interaction(**kwargs) -> Interaction:
    defaults = dict(session_id="s", question_id="q", topic="RNN")
    defaults.update(kwargs)
    return Interaction(**defaults)


def test_report_metrics():
    interactions = [
        _interaction(first_verdict="correct", final_verdict="correct", groundedness=1.0),
        _interaction(
            first_verdict="incorrect", final_verdict="correct",
            hints_shown=1, groundedness=1.0, topic="勾配計算",
        ),
        _interaction(
            first_verdict="incorrect", final_verdict="incorrect",
            hints_shown=2, gave_up=True, groundedness=0.5, topic="softmax",
        ),
    ]
    report = build_report(interactions)
    assert report.attempted == 3
    assert abs(report.first_correct_rate - 1 / 3) < 1e-9
    assert abs(report.final_correct_rate - 2 / 3) < 1e-9
    assert report.hints_shown == 3
    assert report.hint_effective_rate == 0.5  # ヒントを見た2問中1問が正解到達
    assert abs(report.groundedness - 2.5 / 3) < 1e-9
    assert "softmax" in report.weak_topic_notes and "勾配計算" in report.weak_topic_notes


def test_leak_rate_counts_pre_regeneration():
    interactions = [
        _interaction(final_verdict="correct", hints_shown=3, leaked_hint_regenerations=1)
    ]
    report = build_report(interactions)
    assert report.leak_rate == 0.25  # 漏洩1 / (提示3 + 漏洩1)


def test_empty_report():
    report = build_report([])
    assert report.attempted == 0
    assert "0" in report.render()


def test_render_matches_dialogue_example_format():
    report = build_report(
        [_interaction(first_verdict="correct", final_verdict="correct", groundedness=1.0)]
    )
    text = report.render()
    assert "===== セッション評価レポート =====" in text
    assert "初回正答率" in text
    assert "答え漏洩率" in text
    assert "(再生成で抑止済み)" in text


def test_interrupted_report_is_marked():
    report = build_report(
        [_interaction(first_verdict="incorrect", final_verdict="incorrect")],
        completed=False,
    )
    assert "(中断)" in report.render()


def test_evaluate_judge_offline(tmp_path, demo_llm, demo_questions):
    from qqquestion.judge import judge_answer

    question = demo_questions[0]
    eval_set = [
        {"question": question.model_dump(), "answer": question.model_answer,
         "expected": "correct"},
        {"question": question.model_dump(), "answer": "まったく違う話",
         "expected": "incorrect"},
        {"question": question.model_dump(), "answer": "再帰結合があるから",
         "expected": "partial"},
    ]
    path = tmp_path / "eval_set.json"
    path.write_text(json.dumps(eval_set, ensure_ascii=False))

    result = evaluate_judge(lambda q, a: judge_answer(demo_llm, q, a), path)
    assert result["total"] == 3
    assert result["accuracy"] == 1.0, result["failures"]


def test_evaluate_judge_reports_failures(tmp_path, demo_questions):
    question = demo_questions[0]
    path = tmp_path / "eval_set.json"
    path.write_text(json.dumps(
        [{"question": question.model_dump(), "answer": "x", "expected": "correct"}],
        ensure_ascii=False,
    ))
    result = evaluate_judge(
        lambda q, a: Judgement(verdict="incorrect", reason="r"), path
    )
    assert result["accuracy"] == 0.0
    assert result["failures"][0]["expected"] == "correct"
