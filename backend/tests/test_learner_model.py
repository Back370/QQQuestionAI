from qqquestion.learner_model import HistoryStore, LearnerState, load_learner_state
from qqquestion.models import Interaction


def _interaction(topic: str, final: str, session: str = "s1") -> Interaction:
    return Interaction(
        session_id=session,
        question_id="q1",
        topic=topic,
        final_verdict=final,
        first_verdict=final,
    )


def test_history_roundtrip(tmp_path):
    store = HistoryStore(tmp_path / "history.jsonl")
    store.append(_interaction("RNN", "correct"))
    store.append(_interaction("勾配計算", "incorrect"))
    loaded = store.load()
    assert len(loaded) == 2
    assert loaded[0].topic == "RNN"
    assert loaded[1].final_correct is False


def test_corrupt_lines_are_skipped(tmp_path):
    path = tmp_path / "history.jsonl"
    store = HistoryStore(path)
    store.append(_interaction("RNN", "correct"))
    with path.open("a") as f:
        f.write("{broken json\n")
    store.append(_interaction("RNN", "correct"))
    assert len(store.load()) == 2


def test_topic_scores_and_weak_topics():
    history = [
        _interaction("RNN", "correct"),
        _interaction("RNN", "correct"),
        _interaction("勾配計算", "incorrect"),
        _interaction("勾配計算", "incorrect"),
        _interaction("勾配計算", "correct"),
        _interaction("softmax", "incorrect"),
    ]
    state = LearnerState.from_history(history)
    assert state.topic_scores["RNN"] == 1.0
    assert state.weak_topics() == ["softmax", "勾配計算"]  # 正答率が低い順


def test_initial_hint_level():
    state = LearnerState.from_history(
        [_interaction("勾配計算", "incorrect"), _interaction("RNN", "correct")]
    )
    assert state.initial_hint_level("勾配計算") == 2  # 苦手は Lv2 から
    assert state.initial_hint_level("RNN") == 1
    assert state.initial_hint_level("未知トピック") == 1


def test_difficulty_bias():
    state = LearnerState.from_history(
        [_interaction("RNN", "correct")] * 8 + [_interaction("RNN", "incorrect")] * 2
    )
    assert state.difficulty_bias() == {"RNN": 2}  # 正答率 70% 超で +1


def test_load_learner_state_missing_file(tmp_path):
    state = load_learner_state(tmp_path / "none.jsonl")
    assert state.topic_scores == {}
    assert state.weak_topics() == []
