"""正誤判定（architecture.md §5.2 (b)）。

- 許容解答との正規化文字列一致で決まる場合は LLM を呼ばない
- LLM 判定は temperature 0.0、採点基準（rubric / accepted_points）との
  照合として行い、判定理由を必須にする。理由が空なら1回だけ再判定
- 部分正解の要点は attempt をまたいで持ち越す: 前回までに満たした要点
  （already_matched）は再度の言及を要求せず、残りが埋まれば correct にする
"""

from __future__ import annotations

from typing import Iterator, Sequence

from .llm import StreamEvent, StructuredLLM, stream_generate
from .models import Judgement, Question
from .textutil import normalize

_SYSTEM = """あなたは採点者です。学習者の記述式解答を、出題時に確定した採点基準に
照らして判定してください。自分の知識で正解を作り直さず、与えられた
model_answer / accepted_points / rubric だけを根拠にすること。

- verdict: 採点基準の要点をすべて満たせば "correct"、一部なら "partial"、
  ほぼ満たさなければ "incorrect"
- matched_points / missing_points: accepted_points のうち満たした/欠けた要点
- reason: どの要点がどう満たされた/欠けたか、採点基準を参照して必ず書く

表記ゆれ・言い回しの違いは意味が同じなら正解として扱う。
"""


def _exact_match(question: Question, answer: str) -> bool:
    """短い事実解答向けのフォールバック。正規化して完全一致のみ。"""
    normalized_answer = normalize(answer)
    if not normalized_answer:
        return False
    candidates = [question.model_answer, *question.accepted_points]
    return any(normalize(candidate) == normalized_answer for candidate in candidates)


def _canonical_point(point: str, accepted_points: Sequence[str]) -> str | None:
    """LLM が返した要点文字列を accepted_points の正規の1つに対応付ける。"""
    normalized = normalize(point)
    if not normalized:
        return None
    for accepted in accepted_points:
        normalized_accepted = normalize(accepted)
        if (
            normalized == normalized_accepted
            or normalized in normalized_accepted
            or normalized_accepted in normalized
        ):
            return accepted
    return None


def _merge_with_previous(
    question: Question, judgement: Judgement, already_matched: Sequence[str]
) -> Judgement:
    """前回までに満たした要点と合算し、verdict を決定的に再計算する。"""
    matched: list[str] = []
    for point in [*already_matched, *judgement.matched_points]:
        canonical = _canonical_point(point, question.accepted_points)
        if canonical is not None and canonical not in matched:
            matched.append(canonical)
    missing = [p for p in question.accepted_points if p not in matched]

    if judgement.verdict == "correct" and not already_matched:
        return judgement  # 単独で正解ならそのまま（合算で降格はさせない）
    if not missing:
        reason = judgement.reason
        if already_matched:
            reason = (reason + " 前回までの解答と合わせて全要点を満たしました。").strip()
        return Judgement(
            verdict="correct", matched_points=matched, missing_points=[], reason=reason
        )
    if matched:
        return Judgement(
            verdict="partial",
            matched_points=matched,
            missing_points=missing,
            reason=judgement.reason,
        )
    return judgement


def judge_answer_stream(
    llm: StructuredLLM,
    question: Question,
    answer: str,
    already_matched: Sequence[str] = (),
) -> Iterator[StreamEvent]:
    """判定をストリーミングする。

    ("judgement_partial", {"reason": str}) を逐次 yield し、
    最後に ("judgement", Judgement) を必ず1回 yield する。

    reason は verdict が correct / partial と確定した部分に限って流す。
    incorrect の理由には欠けた要点（＝答えの手がかり）が含まれるため、
    UI が非表示にしている情報を途中経過でも漏らさない。
    """
    if not answer.strip():
        yield (
            "judgement",
            Judgement(
                verdict="incorrect",
                matched_points=list(already_matched),
                reason="解答が空です。",
            ),
        )
        return

    if _exact_match(question, answer):
        yield (
            "judgement",
            Judgement(
                verdict="correct",
                matched_points=list(question.accepted_points),
                reason="許容解答と一致",
            ),
        )
        return

    already_note = ""
    if already_matched:
        already_note = (
            f"\n前回までの解答で既に満たした要点（今回の解答に含まれていなくてもよい。"
            f"再度の言及を要求しないこと）: {list(already_matched)}\n"
            "今回はまだ満たされていない要点だけを判定すること。"
        )
    user = (
        f"問題: {question.text}\n"
        f"模範解答: {question.model_answer}\n"
        f"要点(accepted_points): {question.accepted_points}\n"
        f"採点基準(rubric): {question.rubric}\n"
        f"{already_note}\n"
        f"学習者の解答: {answer}"
    )
    judgement: Judgement | None = None
    for name, payload in stream_generate(llm, Judgement, _SYSTEM, user, temperature=0.0):
        if name == "final":
            judgement = payload  # type: ignore[assignment]
            continue
        partial: dict = payload  # type: ignore[assignment]
        reason = partial.get("reason")
        if (
            partial.get("verdict") in ("correct", "partial")
            and isinstance(reason, str)
            and reason
        ):
            yield ("judgement_partial", {"reason": reason})
    assert judgement is not None
    if not judgement.reason.strip():
        # 判定理由の必須化（architecture.md §4.3）: 理由なしは再判定する
        judgement = llm.generate(
            Judgement,
            _SYSTEM + "\nreason は空にできません。必ず採点基準を参照して書くこと。",
            user,
            temperature=0.0,
        )
    if question.accepted_points:
        judgement = _merge_with_previous(question, judgement, already_matched)
    yield ("judgement", judgement)


def judge_answer(
    llm: StructuredLLM,
    question: Question,
    answer: str,
    already_matched: Sequence[str] = (),
) -> Judgement:
    for name, payload in judge_answer_stream(llm, question, answer, already_matched):
        if name == "judgement":
            return payload  # type: ignore[return-value]
    raise AssertionError("judge_answer_stream が judgement を返しませんでした")
