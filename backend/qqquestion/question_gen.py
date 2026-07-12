"""出題生成（architecture.md §5.2 (a)）。

5問構成（前提知識×2 → 実装の説明×3）を生成側プロンプトで要求し、
返答が構成を満たさない場合はコード側で並べ替え・補正して強制する。
模範解答・許容解答・採点基準を出題時に確定させるのが
ハルシネーション抑制の要（判定時の自由生成を減らす）。
"""

from __future__ import annotations

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

選択式にしない。日本語で出題する。
"""


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
    return _force_structure(result.questions)
