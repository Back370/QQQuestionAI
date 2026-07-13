"""出題生成（architecture.md §5.2 (a)）。

5問構成（前提知識×2 → 実装の説明×3）を生成側プロンプトで要求し、
返答が構成を満たさない場合はコード側で並べ替え・補正して強制する。
模範解答・許容解答・採点基準を出題時に確定させるのが
ハルシネーション抑制の要（判定時の自由生成を減らす）。
"""

from __future__ import annotations

import re

from .llm import StructuredLLM
from .models import DiffContext, Question, QuestionSet

PERSONA = (
    "あなたは「答えを絶対に教えず、ヒントだけで理解に導く教師」です。"
    "学習者が自分で書いたコードを本当に理解しているかを確かめます。"
)

_SYSTEM = PERSONA + """
与えられたコミット差分から、記述式の理解度確認問題をちょうど5問生成してください。

構成の厳守:
- 第1〜2問: type="prerequisite"（差分が前提とする基礎知識を問う）
- 第3〜5問: type="implementation"（差分の特定箇所が何をしているか・なぜそう書くかを問う。
  code_snippet に差分から該当コードを引用すること）

各問には必ず以下を含める:
- model_answer: 模範解答
- accepted_points: 正解と認めるために解答に含まれるべき要点のリスト（2〜4個）
- rubric: 採点基準（どの要点が揃えば correct / 一部なら partial かを明文化）
- topic: 問題のトピック名（与えられたトピック候補から選ぶか近いものを付ける）
- difficulty: 1〜3

出題形式の厳守（記述式の一問一答）:
- 1問につき問いは1つだけ。「それぞれ説明してください」「〜ですか？また、〜ですか？」のように
  複数の論点を1問に束ねない
- 疑問文は各問に1つまで。聞きたいことが複数あるなら、最も本質的な1つに絞る

選択式にしない。日本語で出題する。
"""

_REWRITE_SYSTEM = PERSONA + """
次の問題は複数の問いを1問に束ねてしまっています（一問一答の違反）。
最も本質的な問い1つだけに絞って書き直してください。

- 疑問文は1つだけにする。「それぞれ」「また〜も」を使わない
- model_answer / accepted_points / rubric も絞った問いに対応させる
- id / type / topic / difficulty / code_snippet は変えない
"""

# 「？が2つ以上」「それぞれ」「？の直後に また/さらに/加えて」を複数質問とみなす
_QMARK_RE = re.compile(r"[？?]")
_FOLLOWUP_RE = re.compile(r"[？?][、\s]*(また|さらに|加えて)")


def is_multi_question(text: str) -> bool:
    """1問に複数の問いが束ねられていないかのルールベース判定。"""
    if len(_QMARK_RE.findall(text)) >= 2:
        return True
    if "それぞれ" in text:
        return True
    return bool(_FOLLOWUP_RE.search(text))


def _rewrite_as_single_question(llm: StructuredLLM, question: Question) -> Question:
    rewritten = llm.generate(
        Question,
        _REWRITE_SYSTEM,
        question.model_dump_json(),
        temperature=0.2,
    )
    # id 等の同一性はコード側で強制する（LLM の書き換えを信用しない）
    return rewritten.model_copy(
        update={
            "id": question.id,
            "type": question.type,
            "topic": question.topic,
            "difficulty": question.difficulty,
            "code_snippet": question.code_snippet,
        }
    )


def _force_structure(questions: list[Question]) -> list[Question]:
    """前提知識×2 → 実装×3 の並びと id (q1..q5) を強制する。"""
    prerequisites = [q for q in questions if q.type == "prerequisite"]
    implementations = [q for q in questions if q.type == "implementation"]

    ordered = prerequisites[:2] + implementations[:3]
    # 不足分は残り物から型を付け替えて充当する（LLM が構成を守らなかった場合の保険）
    leftovers = prerequisites[2:] + implementations[3:]
    while len(ordered) < 5 and leftovers:
        filler = leftovers.pop(0)
        needed_type = "prerequisite" if sum(
            1 for q in ordered if q.type == "prerequisite"
        ) < 2 else "implementation"
        ordered.append(filler.model_copy(update={"type": needed_type}))
    ordered.sort(key=lambda q: 0 if q.type == "prerequisite" else 1)

    return [q.model_copy(update={"id": f"q{i + 1}"}) for i, q in enumerate(ordered)]


def generate_questions(
    llm: StructuredLLM,
    diff_ctx: DiffContext,
    weak_topics: list[str] | None = None,
    difficulty_bias: dict[str, int] | None = None,
) -> list[Question]:
    weak_note = ""
    if weak_topics:
        weak_note = (
            "\n学習者の苦手トピック（優先的に出題すること）: "
            + ", ".join(weak_topics)
        )
    difficulty_note = ""
    if difficulty_bias:
        difficulty_note = "\nトピック別の推奨難易度: " + ", ".join(
            f"{topic}={level}" for topic, level in difficulty_bias.items()
        )

    user = (
        f"トピック候補: {', '.join(diff_ctx.topics) or '(差分から推定)'}"
        f"{weak_note}{difficulty_note}\n\n"
        f"コミット差分:\n```diff\n{diff_ctx.diff_text[:12000]}\n```"
    )
    result = llm.generate(QuestionSet, _SYSTEM, user, temperature=0.4)
    if len(result.questions) < 5:
        raise ValueError(f"出題が5問未満です: {len(result.questions)}問")
    questions = _force_structure(result.questions)
    # 一問一答の強制: 複数の問いを束ねた問題は1問に絞って書き直させる
    return [
        _rewrite_as_single_question(llm, q) if is_multi_question(q.text) else q
        for q in questions
    ]
