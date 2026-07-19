"""出題生成（architecture.md §5.2 (a)）。

5問構成（前提知識×2 → 実装の説明×3）を生成側プロンプトで要求し、
返答が構成を満たさない場合はコード側で並べ替え・補正して強制する。
模範解答・許容解答・採点基準を出題時に確定させるのが
ハルシネーション抑制の要（判定時の自由生成を減らす）。
"""

from __future__ import annotations

import re
from typing import Iterator

from pydantic import ValidationError

from .llm import StructuredLLM, fast_generate, stream_generate
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


# 5問構成: 前提知識×2 → 実装の説明×3（architecture.md §5.2 (a)）
EXPECTED_TYPES = (
    "prerequisite",
    "prerequisite",
    "implementation",
    "implementation",
    "implementation",
)
TOTAL_QUESTIONS = len(EXPECTED_TYPES)


def _fill_structure(start: int, leftovers: list[Question]) -> list[Question]:
    """先頭 start 問が確定済みの前提で、残りスロットを型に合わせて埋める。

    不足分は残り物の型を付け替えて充当する（LLM が構成を守らなかった場合の保険）。
    """
    prerequisites = [q for q in leftovers if q.type == "prerequisite"]
    implementations = [q for q in leftovers if q.type == "implementation"]
    filled: list[Question] = []
    for slot, needed_type in enumerate(EXPECTED_TYPES[start:], start=start):
        pool, other = (
            (prerequisites, implementations)
            if needed_type == "prerequisite"
            else (implementations, prerequisites)
        )
        if pool:
            question = pool.pop(0)
        elif other:
            question = other.pop(0).model_copy(update={"type": needed_type})
        else:
            break
        filled.append(question.model_copy(update={"id": f"q{slot + 1}"}))
    return filled


def _build_user(
    diff_ctx: DiffContext,
    weak_topics: list[str] | None,
    difficulty_bias: dict[str, int] | None,
) -> str:
    weak_note = ""
    if weak_topics:
        weak_note = (
            "\n学習者の苦手トピック（優先的に出題すること）: "
            + ", ".join(weak_topics)
        )
    difficulty_note = ""
    if difficulty_bias:
        difficulty_note = "\nトピック別の推奨難易度（1=易しめ〜3=難しめ）: " + ", ".join(
            f"{topic}={level}" for topic, level in difficulty_bias.items()
        )
    return (
        f"トピック候補: {', '.join(diff_ctx.topics) or '(差分から推定)'}"
        f"{weak_note}{difficulty_note}\n\n"
        f"コミット差分:\n```diff\n{diff_ctx.diff_text[:12000]}\n```"
    )


def _stream_question_set(
    llm: StructuredLLM, system: str, user: str, start_slot: int
) -> Iterator[Question]:
    """QuestionSet ストリームから start_slot 以降のスロットを1問ずつ確定させる。

    ストリーム途中では、構成（EXPECTED_TYPES）と一問一答を満たす問題だけを
    先行して確定させる。満たさない問題が現れたら先行確定を止め、残りは
    全問そろってから並べ替え・書き直しで補正する。
    """
    expected = EXPECTED_TYPES[start_slot:]
    published = 0
    frozen = False  # 構成違反を見つけたら先行確定をやめて最終補正に回す
    final_set: QuestionSet | None = None
    for name, payload in stream_generate(llm, QuestionSet, system, user, temperature=0.4):
        if name == "final":
            final_set = payload  # type: ignore[assignment]
            continue
        if frozen or not isinstance(payload, dict):
            continue
        items = payload.get("questions")
        if not isinstance(items, list):
            continue
        # 配列の最後の要素は生成途中の可能性があるため、その手前までを確定候補にする
        for index in range(published, min(len(items) - 1, len(expected))):
            try:
                question = Question.model_validate(items[index])
            except ValidationError:
                frozen = True
                break
            if question.type != expected[index] or is_multi_question(question.text):
                frozen = True
                break
            published += 1
            yield question.model_copy(update={"id": f"q{start_slot + published}"})
    assert final_set is not None
    if len(final_set.questions) < len(expected):
        raise ValueError(f"出題が{len(expected)}問未満です: {len(final_set.questions)}問")
    # 部分パースで確定した問題は最終リストの先頭 published 件と同一
    for question in _fill_structure(start_slot + published, final_set.questions[published:]):
        # 一問一答の強制: 複数の問いを束ねた問題は1問に絞って書き直させる
        if is_multi_question(question.text):
            question = _rewrite_as_single_question(llm, question)
        yield question


_FIRST_SYSTEM = PERSONA + """
与えられたコミット差分から、記述式の理解度確認問題を1問だけ生成してください。
これは全5問の第1問で、type="prerequisite"（差分が前提とする基礎知識を問う）とします。

必ず以下を含める:
- model_answer: 模範解答
- accepted_points: 正解と認めるために解答に含まれるべき要点のリスト（2〜4個）
- rubric: 採点基準（どの要点が揃えば correct / 一部なら partial かを明文化）
- topic: 問題のトピック名（与えられたトピック候補から選ぶか近いものを付ける）
- difficulty: 1〜3

出題形式の厳守（記述式の一問一答）:
- 問いは1つだけ。複数の論点を束ねず、疑問文は1つまで
- 選択式にしない。日本語で出題する
"""

_REST_SYSTEM = PERSONA + """
与えられたコミット差分から、記述式の理解度確認問題をちょうど4問生成してください。
全5問のうち第1問は出題済みで、残りの第2〜5問を作ります。

構成の厳守:
- 1問目(第2問): type="prerequisite"（差分が前提とする基礎知識を問う）
- 2〜4問目(第3〜5問): type="implementation"（差分の特定箇所が何をしているか・
  なぜそう書くかを問う。code_snippet に差分から該当コードを引用すること）

出題済みの第1問と重複する内容を出さないこと。

各問には必ず以下を含める:
- model_answer: 模範解答
- accepted_points: 正解と認めるために解答に含まれるべき要点のリスト（2〜4個）
- rubric: 採点基準（どの要点が揃えば correct / 一部なら partial かを明文化）
- topic: 問題のトピック名（与えられたトピック候補から選ぶか近いものを付ける）
- difficulty: 1〜3

出題形式の厳守（記述式の一問一答）:
- 1問につき問いは1つだけ。複数の論点を束ねず、疑問文は各問1つまで
- 選択式にしない。日本語で出題する
"""


def generate_first_question(
    llm: StructuredLLM,
    diff_ctx: DiffContext,
    weak_topics: list[str] | None = None,
    difficulty_bias: dict[str, int] | None = None,
) -> Question:
    """第1問（前提知識）だけを先行生成する。UI はこれで出題を始められる。

    体感待ち時間に直結するため速度優先（Gemini では thinking 無効）で生成する。
    """
    user = _build_user(diff_ctx, weak_topics, difficulty_bias)
    question = fast_generate(llm, Question, _FIRST_SYSTEM, user, temperature=0.4)
    question = question.model_copy(update={"id": "q1", "type": "prerequisite"})
    if is_multi_question(question.text):
        question = _rewrite_as_single_question(llm, question)
    return question


def generate_remaining_questions_stream(
    llm: StructuredLLM,
    diff_ctx: DiffContext,
    first_question: Question,
    weak_topics: list[str] | None = None,
    difficulty_bias: dict[str, int] | None = None,
) -> Iterator[Question]:
    """第2〜5問を生成し、確定した問題から1問ずつ yield する。

    第1問の解答中にバックグラウンドで呼ばれる想定。
    """
    user = (
        _build_user(diff_ctx, weak_topics, difficulty_bias)
        + f"\n\n出題済みの第1問（重複しないこと）:\n{first_question.text}"
    )
    yield from _stream_question_set(llm, _REST_SYSTEM, user, start_slot=1)


def generate_questions_stream(
    llm: StructuredLLM,
    diff_ctx: DiffContext,
    weak_topics: list[str] | None = None,
    difficulty_bias: dict[str, int] | None = None,
) -> Iterator[Question]:
    """5問を1回の生成でストリームする（一括経路）。確定した問題から yield する。"""
    user = _build_user(diff_ctx, weak_topics, difficulty_bias)
    yield from _stream_question_set(llm, _SYSTEM, user, start_slot=0)


def generate_questions(
    llm: StructuredLLM,
    diff_ctx: DiffContext,
    weak_topics: list[str] | None = None,
    difficulty_bias: dict[str, int] | None = None,
) -> list[Question]:
    return list(
        generate_questions_stream(llm, diff_ctx, weak_topics, difficulty_bias)
    )
