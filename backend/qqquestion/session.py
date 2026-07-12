"""クイズセッションの状態機械（architecture.md §3 データフロー 4〜8）。

出題 → 解答 → 判定 → (不正解: ヒント) → (正解/ギブアップ: 解説) → 次の問題
を1クラスで管理する。UI（CLI / FastAPI / Webview）はこのクラスを叩くだけ。
コミット続行条件は「5問の完走」であり全問正解ではない（§1）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from .explainer import generate_explanation
from .hint_gen import generate_hint
from .judge import judge_answer
from .knowledge_base import KnowledgeBase
from .learner_model import HistoryStore, LearnerState
from .llm import StructuredLLM
from .models import (
    DiffContext,
    Explanation,
    Hint,
    Interaction,
    Judgement,
    Question,
)
from .question_gen import generate_questions


@dataclass
class QuestionState:
    question: Question
    interaction: Interaction
    hint_level: int
    done: bool = False


@dataclass
class AnswerResult:
    judgement: Judgement
    explanation: Explanation | None = None  # 正解時に付く
    question_done: bool = False
    model_answer: str | None = None  # 問題が終わったときだけ開示


class QuizSession:
    def __init__(
        self,
        llm: StructuredLLM,
        kb: KnowledgeBase,
        diff_ctx: DiffContext,
        learner_state: LearnerState | None = None,
        history_store: HistoryStore | None = None,
        session_id: str | None = None,
    ):
        self.id = session_id or uuid.uuid4().hex[:12]
        self._llm = llm
        self._kb = kb
        self._history_store = history_store
        self._learner = learner_state or LearnerState()
        self.diff_ctx = diff_ctx
        self.status = "in_progress"  # in_progress | completed | aborted

        questions = generate_questions(
            llm,
            diff_ctx,
            weak_topics=self._learner.weak_topics(),
            difficulty_bias=self._learner.difficulty_bias(),
        )
        self._states = [
            QuestionState(
                question=q,
                interaction=Interaction(
                    session_id=self.id,
                    question_id=q.id,
                    topic=q.topic,
                    difficulty=q.difficulty,
                    question_type=q.type,
                ),
                hint_level=self._learner.initial_hint_level(q.topic),
            )
            for q in questions
        ]
        self._index = 0

    # ---- 参照系 -------------------------------------------------------

    @property
    def finished(self) -> bool:
        return self._index >= len(self._states)

    @property
    def total(self) -> int:
        return len(self._states)

    def current(self) -> QuestionState:
        if self.finished:
            raise IndexError("全問終了しています")
        return self._states[self._index]

    def current_public(self) -> dict | None:
        """UI に返す現在の問題（模範解答なし）。終了後は None。"""
        if self.finished:
            return None
        state = self.current()
        view = state.question.public_view()
        view["number"] = self._index + 1
        view["total"] = self.total
        view["hint_level"] = state.hint_level
        return view

    # ---- 操作系 -------------------------------------------------------

    def submit_answer(self, answer: str) -> AnswerResult:
        state = self.current()
        judgement = judge_answer(self._llm, state.question, answer)

        state.interaction.attempts += 1
        if state.interaction.first_verdict is None:
            state.interaction.first_verdict = judgement.verdict
        state.interaction.final_verdict = judgement.verdict

        if judgement.verdict == "correct":
            explanation = self._finish_question(state, gave_up=False)
            return AnswerResult(
                judgement=judgement,
                explanation=explanation,
                question_done=True,
                model_answer=state.question.model_answer,
            )
        return AnswerResult(judgement=judgement)

    def request_hint(self) -> Hint:
        state = self.current()
        hint, leaks = generate_hint(
            self._llm,
            self._kb,
            state.question,
            user_answer="(未回答またはヒント要求)",
            hint_level=state.hint_level,
        )
        state.interaction.hints_shown += 1
        state.interaction.max_hint_level = max(
            state.interaction.max_hint_level, state.hint_level
        )
        state.interaction.leaked_hint_regenerations += leaks
        state.hint_level = min(4, state.hint_level + 1)  # 再要求ごとに +1
        return hint

    def give_up(self) -> AnswerResult:
        state = self.current()
        state.interaction.gave_up = True
        if state.interaction.first_verdict is None:
            state.interaction.first_verdict = "incorrect"
        state.interaction.final_verdict = "incorrect"
        explanation = self._finish_question(state, gave_up=True)
        return AnswerResult(
            judgement=Judgement(verdict="incorrect", reason="ギブアップ"),
            explanation=explanation,
            question_done=True,
            model_answer=state.question.model_answer,
        )

    def abort(self) -> None:
        """パネルを閉じる等の明示的な中断。コミットは中止される。"""
        if self.status == "in_progress":
            self.status = "aborted"

    # ---- 内部 ---------------------------------------------------------

    def _finish_question(self, state: QuestionState, gave_up: bool) -> Explanation:
        explanation, groundedness = generate_explanation(
            self._llm, self._kb, state.question
        )
        state.interaction.groundedness = groundedness
        state.done = True
        if self._history_store is not None:
            self._history_store.append(state.interaction)
        self._index += 1
        if self.finished:
            self.status = "completed"
        return explanation

    # ---- レポート -----------------------------------------------------

    def interactions(self) -> list[Interaction]:
        return [s.interaction for s in self._states if s.done]

    def report(self):
        from .evaluator import build_report

        return build_report(self.interactions(), completed=self.status == "completed")
