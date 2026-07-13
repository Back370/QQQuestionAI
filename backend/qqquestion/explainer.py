"""解説生成（architecture.md §5.2 (d)）と Groundedness の計測。

解説は正解時・ギブアップ時の両方で提示する。主張は知識ベースの
チャンクに存在する内容に限定し、出典URLを citations で返す。
Groundedness は「解説中の文のうち、内容語の3割以上がいずれかの
チャンクに現れる文の割合」で近似する（evaluator が集計に使う）。
"""

from __future__ import annotations

import re
from typing import Iterator

from .knowledge_base import KnowledgeBase
from .llm import StreamEvent, StructuredLLM, stream_generate
from .models import Chunk, Explanation, Question
from .question_gen import PERSONA

_SYSTEM = PERSONA + """
問題が終わった学習者（正解した、またはギブアップした）に解説を書いてください。

ルール:
- 与えられた模範解答と参考資料(チャンク)にある内容だけで構成する。
  資料にない事実・年号・固有名詞を足さない
- 参考にしたチャンクの出典URLを citations に必ず入れる
- なぜその答えになるか、覚えるための着眼点を1つ添える
- 日本語で簡潔に（3〜6文程度）
"""

_SENTENCE_RE = re.compile(r"[^。．\n!?！？]+[。．!?！？]?")
_WORD_RE = re.compile(r"[A-Za-z_]{3,}|[一-鿿゠-ヿ]{2,}")


def groundedness(explanation_text: str, chunks: list[Chunk], question: Question) -> float:
    """解説の各文が出典（チャンク＋模範解答）に根拠を持つ割合。"""
    sources = " ".join(chunk.text for chunk in chunks)
    sources += " " + question.model_answer + " " + " ".join(question.accepted_points)
    if question.code_snippet:
        sources += " " + question.code_snippet
    sources += " " + question.text

    sentences = [s.strip() for s in _SENTENCE_RE.findall(explanation_text) if s.strip()]
    if not sentences:
        return 0.0
    grounded = 0
    for sentence in sentences:
        words = _WORD_RE.findall(sentence)
        if not words:
            grounded += 1
            continue
        hit = sum(1 for word in words if word in sources)
        if hit / len(words) >= 0.3:
            grounded += 1
    return grounded / len(sentences)


def generate_explanation_stream(
    llm: StructuredLLM, kb: KnowledgeBase, question: Question
) -> Iterator[StreamEvent]:
    """解説をストリーミングする。

    ("explanation_partial", {"explanation": str}) を逐次 yield し、
    最後に ("explanation", (Explanation, groundedness)) を必ず1回 yield する。
    解説は問題終了後（模範解答開示後）にしか呼ばれないため、途中経過を
    そのまま UI に流してよい。
    """
    chunks = kb.query(f"{question.topic} {question.text}", k=4)
    sources = "\n\n".join(
        f"[{i + 1}] {chunk.title} ({chunk.url})\n{chunk.text}"
        for i, chunk in enumerate(chunks)
    ) or "(参考資料なし。模範解答の範囲だけで書くこと)"

    user = (
        f"問題: {question.text}\n"
        + (f"コード:\n{question.code_snippet}\n" if question.code_snippet else "")
        + f"模範解答: {question.model_answer}\n"
        f"要点: {question.accepted_points}\n\n"
        f"参考資料:\n{sources}"
    )
    explanation: Explanation | None = None
    for name, payload in stream_generate(llm, Explanation, _SYSTEM, user, temperature=0.2):
        if name == "final":
            explanation = payload  # type: ignore[assignment]
            continue
        text = payload.get("explanation")  # type: ignore[union-attr]
        if isinstance(text, str) and text:
            yield ("explanation_partial", {"explanation": text})
    assert explanation is not None
    score = groundedness(explanation.explanation, chunks, question)
    yield ("explanation", (explanation, score))


def generate_explanation(
    llm: StructuredLLM, kb: KnowledgeBase, question: Question
) -> tuple[Explanation, float]:
    """解説と groundedness を返す。"""
    for name, payload in generate_explanation_stream(llm, kb, question):
        if name == "explanation":
            return payload  # type: ignore[return-value]
    raise AssertionError("generate_explanation_stream が explanation を返しませんでした")
