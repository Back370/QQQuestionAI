from qqquestion.hint_gen import generate_hint
from qqquestion.models import Hint


def test_hint_returns_citations(fake_llm, kb, demo_questions):
    fake_llm.enqueue(Hint(hint="概念の大枠から考えましょう", citations=["https://example.com/rnn"]))
    hint, leaks = generate_hint(fake_llm, kb, demo_questions[0], "わからない", hint_level=1)
    assert hint.citations == ["https://example.com/rnn"]
    assert leaks == 0
    # 知識ベースのチャンクがプロンプトに渡っている
    assert "再帰結合を持ち" in fake_llm.calls[0]["user"]


def test_leaked_hint_is_regenerated(fake_llm, kb, demo_questions):
    question = demo_questions[0]
    # 1回目は模範解答を漏らすヒント、2回目は安全なヒント
    fake_llm.enqueue(Hint(hint=f"答えは「{question.model_answer}」です"))
    fake_llm.enqueue(Hint(hint="前の時刻の情報がどこへ行くか考えてみましょう...ではなく大枠から"))
    hint, leaks = generate_hint(fake_llm, kb, question, "わからない", hint_level=1)
    assert leaks == 1
    assert question.model_answer not in hint.hint
    assert len(fake_llm.calls) == 2


def test_regeneration_gives_up_after_max(fake_llm, kb, demo_questions):
    question = demo_questions[0]
    for _ in range(10):
        fake_llm.enqueue(Hint(hint=f"正解: {question.model_answer}"))
    hint, leaks = generate_hint(fake_llm, kb, question, "わからない", hint_level=1)
    assert leaks == 3  # MAX_REGENERATIONS で打ち切り、無限ループしない
    assert len(fake_llm.calls) == 4


def test_level4_choices_may_contain_answer(fake_llm, kb, demo_questions):
    question = demo_questions[0]
    fake_llm.enqueue(Hint(hint=f"(A) {question.model_answer} (B) 別の答え (C) さらに別"))
    hint, leaks = generate_hint(fake_llm, kb, question, "わからない", hint_level=4)
    assert leaks == 0  # レベル4（3択）は漏洩チェック対象外
    assert len(fake_llm.calls) == 1


def test_hint_level_is_clamped(fake_llm, kb, demo_questions):
    fake_llm.enqueue(Hint(hint="ほぼ核心のヒント"))
    generate_hint(fake_llm, kb, demo_questions[0], "わからない", hint_level=99)
    assert "ヒントレベル 4" in fake_llm.calls[0]["user"]


def test_partial_hint_targets_only_missing_points(fake_llm, kb, demo_questions):
    """部分正解では、欠けている要点だけをヒントの対象としてLLMに渡す。"""
    question = demo_questions[0]
    matched = [question.accepted_points[0]]
    missing = question.accepted_points[1:]
    fake_llm.enqueue(Hint(hint="残っている観点だけ考えてみましょう"))
    generate_hint(
        fake_llm,
        kb,
        question,
        "再帰結合があること",
        hint_level=1,
        matched_points=matched,
        missing_points=missing,
    )
    call = fake_llm.calls[0]
    assert f"既に満たせている要点(ヒントを出してはいけない): {matched}" in call["user"]
    assert f"まだ満たせていない要点(ヒントの対象。内容は漏らさない): {missing}" in call["user"]
    # 満たせている要点にヒントを出さないよう system 側でも指示している
    assert "部分正解" in call["system"]


def test_full_incorrect_hint_has_no_focus_section(fake_llm, kb, demo_questions):
    """丸ごと不正解なら従来どおり問題全体に向けたヒントを出す。"""
    fake_llm.enqueue(Hint(hint="大枠から考えましょう"))
    generate_hint(fake_llm, kb, demo_questions[0], "わからない", hint_level=1)
    call = fake_llm.calls[0]
    assert "既に満たせている要点" not in call["user"]
    assert "部分正解" not in call["system"]


def test_matched_point_in_hint_is_not_treated_as_leak(fake_llm, kb, demo_questions):
    """満たせている要点は学習者自身が書いた内容なので、触れても漏洩ではない。"""
    question = demo_questions[0]
    matched = [question.accepted_points[0]]
    fake_llm.enqueue(Hint(hint=f"{matched[0]}については説明できていますね。残りを考えましょう"))
    hint, leaks = generate_hint(
        fake_llm,
        kb,
        question,
        "再帰結合があること",
        hint_level=1,
        matched_points=matched,
        missing_points=question.accepted_points[1:],
    )
    assert leaks == 0
    assert len(fake_llm.calls) == 1  # 再生成していない


def test_missing_point_in_hint_is_still_a_leak(fake_llm, kb, demo_questions):
    """欠けている要点そのものを言ってしまうのは従来どおり漏洩として弾く。"""
    question = demo_questions[0]
    missing = question.accepted_points[1:]
    fake_llm.enqueue(Hint(hint=f"答えは{missing[0]}です"))
    fake_llm.enqueue(Hint(hint="どこに情報が引き継がれるか考えてみましょう"))
    hint, leaks = generate_hint(
        fake_llm,
        kb,
        question,
        "再帰結合があること",
        hint_level=1,
        matched_points=[question.accepted_points[0]],
        missing_points=missing,
    )
    assert leaks == 1
    assert missing[0] not in hint.hint
