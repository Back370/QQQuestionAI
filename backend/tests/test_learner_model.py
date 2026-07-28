from qqquestion.learner_model import (
    HistoryStore,
    LearnerState,
    format_learner_summary,
    is_related_topic,
    load_learner_state,
)
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
        [_interaction("RNN", "incorrect")] * 2 + [_interaction("RNN", "correct")] * 8
    )
    assert state.difficulty_bias() == {"RNN": 2}  # 正答率 70% 超で難易度を上げる


def test_difficulty_bias_follows_recent_results():
    """直近で落としているなら、通算成績が良くても難易度を上げない。"""
    state = LearnerState.from_history(
        [_interaction("RNN", "correct")] * 8 + [_interaction("RNN", "incorrect")] * 2
    )
    assert state.topic_scores["RNN"] == 0.6  # 直近5件 = 正解3/不正解2
    assert state.difficulty_bias() == {}  # 中間なので既定に委ねる


def test_difficulty_bias_lowers_for_weak_topic():
    # RNN=100%（上げる）, softmax=25%（苦手→下げる）, 中間トピックは対象外
    state = LearnerState.from_history(
        [_interaction("RNN", "correct")] * 4
        + [_interaction("softmax", "incorrect")] * 3
        + [_interaction("softmax", "correct")]
        + [_interaction("中間", "correct")]  # 50%〜70% はエントリを出さない
        + [_interaction("中間", "incorrect")]
    )
    assert state.difficulty_bias() == {"RNN": 2, "softmax": 1}


def test_load_learner_state_missing_file(tmp_path):
    state = load_learner_state(tmp_path / "none.jsonl")
    assert state.topic_scores == {}
    assert state.weak_topics() == []
    assert state.priority_topics(["RNN"]) == []
    assert state.overcome_topics() == []


# ---- 苦手からの克服（#18 苦手傾向の偏り） -----------------------------------


def test_old_failures_drop_out_of_the_recent_window():
    """古い不正解は直近ウィンドウから外れ、正解を積めば苦手判定が外れる。"""
    history = (
        [_interaction("排他制御", "incorrect")] * 3
        + [_interaction("排他制御", "correct")] * 5
    )
    state = LearnerState.from_history(history)
    assert state.topic_scores["排他制御"] == 1.0  # 直近5件だけを見る
    assert state.weak_topics() == []


def test_recent_failures_still_mark_a_topic_weak():
    """直近で落としているトピックは、過去に正解していても苦手のまま。"""
    history = (
        [_interaction("排他制御", "correct")] * 5
        + [_interaction("排他制御", "incorrect")] * 4
    )
    state = LearnerState.from_history(history)
    assert state.weak_topics() == ["排他制御"]


def test_overcome_topics_lists_recovered_topics():
    history = (
        [_interaction("排他制御", "incorrect")] * 2
        + [_interaction("排他制御", "correct")] * 4
        + [_interaction("非同期処理", "incorrect")] * 2  # まだ克服していない
        + [_interaction("キャッシュ", "correct")]  # つまずいたことがない
    )
    state = LearnerState.from_history(history)
    assert state.overcome_topics() == ["排他制御"]


# ---- 差分との関連づけ -------------------------------------------------------


def test_is_related_topic_matches_loosely():
    assert is_related_topic("排他制御", ["並行処理", "排他制御", "キャッシュ"])
    assert is_related_topic("排他制御(Lock)", ["排他制御"])  # LLM 命名のゆれ
    assert not is_related_topic("埋め込み表現", ["並行処理", "排他制御", "キャッシュ"])
    assert not is_related_topic("埋め込み表現", [])


def test_priority_topics_only_includes_diff_related_weak_topics():
    """差分と無関係な苦手トピックは優先出題に回さない（#18）。"""
    history = (
        [_interaction("埋め込み表現", "incorrect")]
        + [_interaction("ベクトル演算", "incorrect")]
        + [_interaction("排他制御", "incorrect")]
    )
    state = LearnerState.from_history(history)
    assert set(state.weak_topics()) == {"埋め込み表現", "ベクトル演算", "排他制御"}
    assert state.priority_topics(["並行処理", "排他制御", "キャッシュ"]) == ["排他制御"]


def test_priority_topics_is_capped():
    history = [
        _interaction(topic, "incorrect")
        for topic in ("排他制御", "並行処理", "キャッシュ", "再帰")
    ]
    state = LearnerState.from_history(history)
    priority = state.priority_topics(["排他制御", "並行処理", "キャッシュ", "再帰"])
    assert len(priority) == 3  # MAX_PRIORITY_TOPICS


def test_difficulty_bias_can_be_limited_to_diff_topics():
    state = LearnerState.from_history(
        [_interaction("RNN", "correct")] * 4 + [_interaction("softmax", "incorrect")] * 2
    )
    assert state.difficulty_bias() == {"RNN": 2, "softmax": 1}
    assert state.difficulty_bias(["softmax"]) == {"softmax": 1}  # 無関係な履歴は落とす


# ---- 表示 -------------------------------------------------------------------


def test_summary_says_which_weak_topics_are_actually_asked():
    lines = format_learner_summary(
        weak_topics=["埋め込み表現", "排他制御"],
        priority_topics=["排他制御"],
        overcome_topics=["RNN"],
        scores={"埋め込み表現": 0.0, "排他制御": 0.4},
    )
    text = "\n".join(lines)
    assert "排他制御(40%) → 今回の差分に関係するので優先出題します" in text
    assert "今回は出題しない苦手: 埋め込み表現(0%)" in text
    assert "克服したトピック: RNN" in text


def test_summary_is_empty_without_history():
    assert format_learner_summary([], [], []) == []
