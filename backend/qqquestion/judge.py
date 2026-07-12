"""正誤判定（architecture.md §5.2 (b)）。

- 許容解答との正規化文字列一致で決まる場合は LLM を呼ばない
- LLM 判定は temperature 0.0、採点基準（rubric / accepted_points）との
  照合として行い、判定理由を必須にする。理由が空なら1回だけ再判定
"""

from __future__ import annotations

from .llm import StructuredLLM
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


def judge_answer(llm: StructuredLLM, question: Question, answer: str) -> Judgement:
    if not answer.strip():
        return Judgement(verdict="incorrect", reason="解答が空です。")

    if _exact_match(question, answer):
        return Judgement(
            verdict="correct",
            matched_points=list(question.accepted_points),
            reason="許容解答と一致",
        )

    user = (
        f"問題: {question.text}\n"
        f"模範解答: {question.model_answer}\n"
        f"要点(accepted_points): {question.accepted_points}\n"
        f"採点基準(rubric): {question.rubric}\n\n"
        f"学習者の解答: {answer}"
    )
    judgement = llm.generate(Judgement, _SYSTEM, user, temperature=0.0)
    if not judgement.reason.strip():
        # 判定理由の必須化（architecture.md §4.3）: 理由なしは再判定する
        judgement = llm.generate(
            Judgement,
            _SYSTEM + "\nreason は空にできません。必ず採点基準を参照して書くこと。",
            user,
            temperature=0.0,
        )
    return judgement
