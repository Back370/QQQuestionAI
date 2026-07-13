from qqquestion.learner_model import HistoryStore, LearnerState
from qqquestion.models import Interaction
from qqquestion.session import QuizSession


def _make_session(demo_llm, kb, diff_ctx, tmp_path, learner_state=None):
    return QuizSession(
        llm=demo_llm,
        kb=kb,
        diff_ctx=diff_ctx,
        learner_state=learner_state,
        history_store=HistoryStore(tmp_path / "history.jsonl"),
    )


CORRECT_ANSWERS = {
    "q1": "隠れ層に再帰結合があり、前の時刻の隠れ状態を使って系列の文脈を保持できる点",
    "q2": "予測確率分布と正解のone-hot分布の間の隔たりを測る",
    "q3": "順伝播を回して隠れ状態と活性化関数の勾配を保存している",
    "q4": "delta[t+1]への依存があるため未来から過去の順で計算する",
    "q5": "Wは前時刻の状態を運ぶ重みで、deltaと1時刻前の状態を対応させるため",
}


def test_full_flow_all_correct(demo_llm, kb, diff_ctx, tmp_path):
    session = _make_session(demo_llm, kb, diff_ctx, tmp_path)
    assert session.total == 5
    assert session.status == "in_progress"

    while not session.finished:
        view = session.current_public()
        result = session.submit_answer(CORRECT_ANSWERS[view["id"]])
        assert result.judgement.verdict == "correct"
        assert result.explanation is not None  # 正解時も解説が付く
        assert result.model_answer

    assert session.status == "completed"
    report = session.report()
    assert report.attempted == 5
    assert report.first_correct_rate == 1.0
    # 履歴が永続化されている
    assert len(HistoryStore(tmp_path / "history.jsonl").load()) == 5


def test_public_view_never_leaks_answer(demo_llm, kb, diff_ctx, tmp_path):
    session = _make_session(demo_llm, kb, diff_ctx, tmp_path)
    view = session.current_public()
    assert "model_answer" not in view
    assert "accepted_points" not in view
    assert "rubric" not in view


def test_wrong_then_hint_then_correct(demo_llm, kb, diff_ctx, tmp_path):
    session = _make_session(demo_llm, kb, diff_ctx, tmp_path)

    result = session.submit_answer("まったくわかりません")
    assert result.judgement.verdict == "incorrect"
    assert result.question_done is False

    hint = session.request_hint()
    assert hint.hint
    state = session.current()
    assert state.interaction.hints_shown == 1
    assert state.hint_level == 2  # 再要求ごとに +1

    result = session.submit_answer(CORRECT_ANSWERS["q1"])
    assert result.judgement.verdict == "correct"

    interaction = session.interactions()[0]
    assert interaction.first_verdict == "incorrect"
    assert interaction.final_verdict == "correct"
    assert interaction.attempts == 2


def test_give_up_reveals_answer_and_moves_on(demo_llm, kb, diff_ctx, tmp_path):
    session = _make_session(demo_llm, kb, diff_ctx, tmp_path)
    result = session.give_up()
    assert result.question_done
    assert result.model_answer  # ギブアップで初めて開示
    assert result.explanation is not None
    assert session.current_public()["id"] == "q2"

    interaction = session.interactions()[0]
    assert interaction.gave_up
    assert interaction.final_verdict == "incorrect"


def test_partial_answers_accumulate_across_attempts(demo_llm, kb, diff_ctx, tmp_path):
    """部分正解 → 足りなかった要素だけ答えれば正解になる（言い直し不要）。"""
    session = _make_session(demo_llm, kb, diff_ctx, tmp_path)

    # q1 の要点は「再帰結合」「前の時刻の隠れ状態」「系列・文脈の保持」の3つ
    first = session.submit_answer("隠れ層が再帰結合を持つ点が違います")
    assert first.judgement.verdict == "partial"
    assert first.question_done is False

    # 2回目は「再帰結合」に触れず、残り2要点だけを答える
    second = session.submit_answer("前の時刻の隠れ状態を使って系列の文脈を保持できる")
    assert second.judgement.verdict == "correct"
    assert second.question_done is True

    interaction = session.interactions()[0]
    assert interaction.first_verdict == "partial"
    assert interaction.final_verdict == "correct"
    assert interaction.attempts == 2


def test_hint_level_starts_at_2_for_weak_topic(demo_llm, kb, diff_ctx, tmp_path):
    history = [
        Interaction(session_id="old", question_id="q", topic="RNN",
                    first_verdict="incorrect", final_verdict="incorrect")
    ]
    learner_state = LearnerState.from_history(history)
    session = _make_session(demo_llm, kb, diff_ctx, tmp_path, learner_state)
    # q1 のトピックは RNN（苦手）なので Lv2 から
    assert session.current().hint_level == 2


def test_abort_marks_session(demo_llm, kb, diff_ctx, tmp_path):
    session = _make_session(demo_llm, kb, diff_ctx, tmp_path)
    session.give_up()
    session.abort()
    assert session.status == "aborted"
    report = session.report()
    assert report.attempted == 1
    assert not report.completed


def test_hint_level_caps_at_4(demo_llm, kb, diff_ctx, tmp_path):
    session = _make_session(demo_llm, kb, diff_ctx, tmp_path)
    for _ in range(6):
        session.request_hint()
    assert session.current().hint_level == 4
    assert session.current().interaction.max_hint_level == 4
