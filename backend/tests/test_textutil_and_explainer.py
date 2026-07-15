from qqquestion.explainer import generate_explanation, groundedness
from qqquestion.models import Chunk, Explanation, Hint
from qqquestion.textutil import contains_answer, normalize


def test_normalize_absorbs_notation():
    assert normalize("１７７６年") == normalize("1776年")
    assert normalize("Ｓｏｆｔ Ｍａｘ！") == normalize("softmax")
    assert normalize("再帰 結合。") == normalize("再帰結合")


def test_contains_answer_detects_leak():
    assert contains_answer("答えはマルティン・ルターです", ["マルティン・ルター"])
    assert contains_answer("正解は 1776 年です", ["1776年"])
    assert not contains_answer("18世紀の出来事です", ["1776年"])


def test_contains_answer_ignores_short_answers():
    # 短すぎる解答は偶然の包含が多いので照合しない
    assert not contains_answer("tとt+1に注目", ["t"], min_len=4)


def test_groundedness_full_when_supported(demo_questions):
    question = demo_questions[0]
    chunks = [Chunk(text="RNN は再帰結合を持ち、前の時刻の隠れ状態を利用する。")]
    score = groundedness("RNNは再帰結合を持ちます。前の時刻の隠れ状態を使います。", chunks, question)
    assert score == 1.0


def test_groundedness_penalizes_unsupported_claims(demo_questions):
    question = demo_questions[0]
    chunks = [Chunk(text="RNN は再帰結合を持つ。")]
    text = "RNNは再帰結合を持ちます。ちなみにこの手法はノーベル物理学賞をシュレディンガーが受賞しました。"
    score = groundedness(text, chunks, question)
    assert score < 1.0


def test_generate_explanation_passes_chunks_and_scores(fake_llm, kb, demo_questions):
    fake_llm.enqueue(
        Explanation(
            explanation="RNNは再帰結合を持ち、前の時刻の隠れ状態を利用します。",
            citations=["https://example.com/rnn"],
        )
    )
    explanation, score = generate_explanation(fake_llm, kb, demo_questions[0])
    assert explanation.citations == ["https://example.com/rnn"]
    assert score == 1.0
    assert fake_llm.calls[0]["temperature"] == 0.2
    assert "再帰結合を持ち" in fake_llm.calls[0]["user"]  # チャンクが根拠として渡る


def test_citations_deduped_preserving_order():
    # 同一URLの複数チャンクを引用すると出典が重複するので、順序を保って除去する
    url = "https://ejje.weblio.jp/content/multiply"
    explanation = Explanation(explanation="解説", citations=[url, url, url, url])
    assert explanation.citations == [url]

    hint = Hint(hint="ヒント", citations=["https://a", "https://b", "https://a"])
    assert hint.citations == ["https://a", "https://b"]
