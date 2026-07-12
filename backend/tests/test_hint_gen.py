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
