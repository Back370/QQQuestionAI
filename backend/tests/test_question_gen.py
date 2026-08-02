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
    # 推奨難易度は system 側の難易度定義と同じ尺度を指すことを明示している
    assert "difficulty の定義" in prompt


def _enqueue_five(fake_llm) -> None:
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


def _assert_difficulty_guide(system: str) -> None:
    """1/2/3 が何を問うレベルかを system プロンプトが定義していること。

    数値だけ渡すと LLM が中央値（2）に寄り、難易度2ばかりが出題される。
    """
    assert "1: 用語や API の意味" in system
    assert "2: そのコードで何が起きるか" in system
    assert "3: なぜその書き方を選んだか" in system


def test_difficulty_guide_in_batch_prompt(fake_llm, diff_ctx):
    _enqueue_five(fake_llm)
    generate_questions(fake_llm, diff_ctx)
    system = fake_llm.calls[0]["system"]
    _assert_difficulty_guide(system)
    assert "難易度1と難易度3をそれぞれ1問以上含める" in system


def test_difficulty_guide_in_first_question_prompt(fake_llm, diff_ctx):
    from qqquestion.question_gen import generate_first_question

    fake_llm.enqueue(_make_question("a", "prerequisite"))
    generate_first_question(fake_llm, diff_ctx)
    system = fake_llm.calls[0]["system"]
    _assert_difficulty_guide(system)
    assert "第1問は導入なので difficulty は 1 か 2 とする" in system


def test_difficulty_guide_in_remaining_questions_prompt(fake_llm, diff_ctx):
    from qqquestion.question_gen import generate_remaining_questions_stream

    first = _make_question("q1", "prerequisite").model_copy(update={"difficulty": 3})
    fake_llm.enqueue(
        QuestionSet(
            questions=[
                _make_question("b", "prerequisite"),
                _make_question("c", "implementation"),
                _make_question("d", "implementation"),
                _make_question("e", "implementation"),
            ]
        )
    )
    list(generate_remaining_questions_stream(fake_llm, diff_ctx, first))
    _assert_difficulty_guide(fake_llm.calls[0]["system"])
    # 第1問の難易度を伝え、残り4問の配分をそれに合わせられるようにする
    assert "難易度3" in fake_llm.calls[0]["user"]


def test_demo_llm_returns_five_rnn_questions(demo_llm, diff_ctx):
    questions = generate_questions(demo_llm, diff_ctx)
    assert len(questions) == 5
    assert all(q.model_answer for q in questions)
    assert all(q.rubric for q in questions)


def test_is_multi_question_detection():
    from qqquestion.question_gen import is_multi_question

    # ユーザー報告の実例: 「それぞれ」+ 連携質問の束ね
    assert is_multi_question(
        "`candidates = ...` の行と `if not candidates` の行は、それぞれどのような"
        "役割を担っていますか？この2行が連携してどのような目的を達成しているか"
        "説明してください。"
    )
    assert is_multi_question("この関数は何をしますか？また、なぜ必要ですか？")
    assert is_multi_question("これは何ですか？ さらに、計算量も述べてください。")
    # 一問一答は通す
    assert not is_multi_question("逆伝播のループで、なぜ reversed で回す必要があるのですか？")
    assert not is_multi_question("「再帰結合」という語を使って説明してください。")


def test_multi_question_is_rewritten(fake_llm, diff_ctx):
    multi = _make_question("a", "prerequisite").model_copy(
        update={"text": "この行は何をしますか？また、なぜ必要ですか？"}
    )
    fake_llm.enqueue(
        QuestionSet(
            questions=[
                multi,
                _make_question("b", "prerequisite"),
                _make_question("c", "implementation"),
                _make_question("d", "implementation"),
                _make_question("e", "implementation"),
            ]
        )
    )
    # 書き直し応答（idやtypeを変えて返してもコード側で元に戻ることを確認）
    fake_llm.enqueue(
        Question(
            id="wrong-id",
            type="implementation",
            text="この行は何をしますか？",
            model_answer="答え",
            accepted_points=["要点"],
            rubric="要点があれば正解",
            topic="別トピック",
            difficulty=3,
        )
    )
    questions = generate_questions(fake_llm, diff_ctx)
    rewritten = questions[0]
    assert rewritten.text == "この行は何をしますか？"
    assert rewritten.id == "q1"                # id は元のまま強制
    assert rewritten.type == "prerequisite"    # type も維持
    assert rewritten.topic == "RNN"
    # 書き直し呼び出しは1回だけ（他の4問は一問一答なので呼ばれない）
    rewrite_calls = [c for c in fake_llm.calls if c["schema"] == "Question"]
    assert len(rewrite_calls) == 1


# ---- 問いと採点の一致（Issue #29） ------------------------------------


def _assert_scope_rule(system: str) -> None:
    """採点の要点を問題文が求める範囲に収めるよう指示していること。

    問題文が聞いていない観点を accepted_points に入れると、学習者は問題文
    どおりに答えても partial にされる（Issue #29）。
    """
    assert "問いと採点の一致の厳守" in system
    assert "問題文が聞いていない観点を採点の要点にしない" in system
    assert "その観点を問題文の側に明記する" in system
    # 観点を書かせる副作用で答えを漏らさせない
    assert "答えの内容" in system


def test_scope_rule_in_batch_prompt(fake_llm, diff_ctx):
    _enqueue_five(fake_llm)
    generate_questions(fake_llm, diff_ctx)
    _assert_scope_rule(fake_llm.calls[0]["system"])


def test_scope_rule_in_first_question_prompt(fake_llm, diff_ctx):
    from qqquestion.question_gen import generate_first_question

    fake_llm.enqueue(_make_question("a", "prerequisite"))
    generate_first_question(fake_llm, diff_ctx)
    _assert_scope_rule(fake_llm.calls[0]["system"])


def test_scope_rule_in_remaining_questions_prompt(fake_llm, diff_ctx):
    from qqquestion.question_gen import generate_remaining_questions_stream

    first = _make_question("q1", "prerequisite")
    fake_llm.enqueue(
        QuestionSet(
            questions=[
                _make_question("b", "prerequisite"),
                _make_question("c", "implementation"),
                _make_question("d", "implementation"),
                _make_question("e", "implementation"),
            ]
        )
    )
    list(generate_remaining_questions_stream(fake_llm, diff_ctx, first))
    _assert_scope_rule(fake_llm.calls[0]["system"])


def test_single_question_rewrite_drops_unasked_points(fake_llm, diff_ctx):
    """束ねた問いを絞るときは、聞かなくなった要点も採点から外させる。"""
    from qqquestion.question_gen import _REWRITE_SYSTEM

    assert "accepted_points から削る" in _REWRITE_SYSTEM


def test_leaks_model_answer_detection():
    from qqquestion.question_gen import leaks_model_answer

    leaking = _make_question("a", "prerequisite").model_copy(
        update={
            "text": (
                "メモリを過剰に消費してシステムが不安定になるのを防ぐために"
                "読み込みサイズを制限しているのはなぜですか。"
            ),
            "model_answer": "メモリを過剰に消費してシステムが不安定になるのを防ぐため",
        }
    )
    assert leaks_model_answer(leaking)

    # 要点の語（「再帰結合」）を問題文で使わせるのは漏洩ではない
    from qqquestion.demo import DEMO_QUESTIONS

    assert not any(leaks_model_answer(q) for q in DEMO_QUESTIONS)


def test_question_leaking_answer_is_rewritten(fake_llm, diff_ctx):
    leaking = _make_question("a", "prerequisite").model_copy(
        update={
            "text": "答えは「読み込みサイズを制限してメモリ枯渇を防ぐこと」ですが、なぜですか。",
            "model_answer": "読み込みサイズを制限してメモリ枯渇を防ぐこと",
        }
    )
    fake_llm.enqueue(
        QuestionSet(
            questions=[
                leaking,
                _make_question("b", "prerequisite"),
                _make_question("c", "implementation"),
                _make_question("d", "implementation"),
                _make_question("e", "implementation"),
            ]
        )
    )
    fake_llm.enqueue(
        Question(
            id="wrong-id",
            type="implementation",
            text="読み込みサイズを制限しているのはなぜですか。",
            model_answer="書き直し側の模範解答（採用されない）",
            accepted_points=["書き直し側の要点（採用されない）"],
            rubric="書き直し側の基準（採用されない）",
            topic="別トピック",
            difficulty=3,
        )
    )
    questions = generate_questions(fake_llm, diff_ctx)
    repaired = questions[0]
    assert repaired.text == "読み込みサイズを制限しているのはなぜですか。"
    # 書き直させるのは問題文だけ。採点側は出題時に確定した値を維持する
    assert repaired.model_answer == "読み込みサイズを制限してメモリ枯渇を防ぐこと"
    assert repaired.accepted_points == ["要点"]
    assert repaired.rubric == "要点があれば正解"
    assert repaired.id == "q1" and repaired.type == "prerequisite"
    assert repaired.topic == "RNN"
    rewrite_calls = [c for c in fake_llm.calls if c["schema"] == "Question"]
    assert len(rewrite_calls) == 1
    assert "答えそのもの" in rewrite_calls[0]["system"]
