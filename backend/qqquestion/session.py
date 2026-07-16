"""クイズセッションの状態機械（architecture.md §3 データフロー 4〜8）。

出題 → 解答 → 判定 → (不正解: ヒント) → (正解/ギブアップ: 解説) → 次の問題
を1クラスで管理する。UI（CLI / FastAPI / Webview）はこのクラスを叩くだけ。
コミット続行条件は「5問の完走」であり全問正解ではない（§1）。
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Iterator

from .explainer import generate_explanation_stream
from .hint_gen import generate_hint
from .judge import judge_answer_stream
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
    QuizOrigin,
)
from .question_gen import (
    TOTAL_QUESTIONS,
    generate_first_question,
    generate_remaining_questions_stream,
)

logger = logging.getLogger(__name__)


@dataclass
class QuestionState:
    question: Question
    interaction: Interaction
    hint_level: int
    done: bool = False
    # 部分正解で満たした要点の蓄積。次の解答では残りの要点だけ埋まれば正解になる
    matched_points: list[str] = field(default_factory=list)


@dataclass
class AnswerResult:
    judgement: Judgement
    explanation: Explanation | None = None  # 正解時に付く
    question_done: bool = False
    model_answer: str | None = None  # 問題が終わったときだけ開示


def _consume(events: Iterator[tuple[str, object]]) -> AnswerResult:
    """ストリームを読み捨てて最終結果だけ返す（ワンショット互換用）。"""
    for name, payload in events:
        if name == "result":
            return payload  # type: ignore[return-value]
    raise AssertionError("ストリームが result イベントを返しませんでした")


class QuizSession:
    def __init__(
        self,
        llm: StructuredLLM,
        kb: KnowledgeBase,
        diff_ctx: DiffContext,
        learner_state: LearnerState | None = None,
        history_store: HistoryStore | None = None,
        session_id: str | None = None,
        defer_questions: bool = False,
        repo_path: str | None = None,
        origin: QuizOrigin = "hook",
    ):
        self.id = session_id or uuid.uuid4().hex[:12]
        self._llm = llm
        self._kb = kb
        self._history_store = history_store
        self._learner = learner_state or LearnerState()
        self.diff_ctx = diff_ctx
        # コミットが走ったリポジトリの絶対パス。/quiz/pending が「どのVSCode
        # ウィンドウにクイズを出すか」をワークスペースと突き合わせて決めるのに使う
        self.repo_path = repo_path
        # 起動元。cli / ui は自分で UI を持っているので、拡張がパネルを開くと
        # 二重表示になる（/quiz/pending は hook 起点だけを返す）
        self.origin: QuizOrigin = origin
        self.status = "in_progress"  # in_progress | completed | aborted
        self.error: str | None = None  # 出題生成に失敗したときのメッセージ

        self._states: list[QuestionState] = []
        self._index = 0
        self._preparing = True
        # defer_questions=True のときは呼び出し側が prepare() を実行する。
        # セッションを先に UI へ公開してから出題を確定させるため（パネル即時表示）
        if not defer_questions:
            self.prepare()

    def prepare(self, fail_open: bool = False) -> None:
        """全問を同期で生成する（CLI・テスト用の互換経路）。

        サーバは prepare_first() → バックグラウンドで prepare_rest() と
        分割して呼び、第1問の表示を待ち時間なしにする。
        """
        self.prepare_first(fail_open=fail_open)
        self.prepare_rest(fail_open=fail_open)

    def prepare_first(self, fail_open: bool = False) -> None:
        """第1問だけを先行生成する。確定した時点で UI は出題を始められる。

        fail_open=True では生成失敗でも例外を投げず、completed にして
        コミットを通す（従来のスキップ相当）。
        """
        try:
            question = generate_first_question(
                self._llm,
                self.diff_ctx,
                weak_topics=self._learner.weak_topics(),
                difficulty_bias=self._learner.difficulty_bias(),
            )
            self._states.append(self._make_state(question))
        except Exception as error:
            # 原因（元例外の連鎖・トレースバック）をログに残す。UI に出る
            # self.error は分類済みメッセージで、生の失敗理由はここでしか追えない
            logger.exception("第1問の生成に失敗しました: session=%s", self.id)
            self._preparing = False
            if not fail_open:
                raise
            self.error = str(error)
            if self.status == "in_progress":
                self.status = "completed"

    def prepare_rest(self, fail_open: bool = False) -> None:
        """第2〜5問を生成し、確定した問題から順に追加する。

        第1問の解答中にバックグラウンドで実行される想定。fail_open=True では
        生成失敗でも例外を投げず、確定済みの問題だけで続行する。
        """
        if not self._states:
            return  # 第1問の生成に失敗している（prepare_first 側で処理済み）
        try:
            for question in generate_remaining_questions_stream(
                self._llm,
                self.diff_ctx,
                self._states[0].question,
                weak_topics=self._learner.weak_topics(),
                difficulty_bias=self._learner.difficulty_bias(),
            ):
                if self.status != "in_progress":
                    return  # 準備中にパネルが閉じられた等
                self._states.append(self._make_state(question))
        except Exception as error:
            logger.exception(
                "残り問題の生成に失敗しました: session=%s 確定済み=%d問", self.id, len(self._states)
            )
            if not fail_open:
                raise
            self.error = str(error)  # 確定済みの問題だけで続行
        finally:
            self._preparing = False
            # 準備完了前にユーザーが確定済みの全問を解き終えていた場合の後始末
            if self.finished and self.status == "in_progress":
                self.status = "completed"

    def _make_state(self, question: Question) -> QuestionState:
        return QuestionState(
            question=question,
            interaction=Interaction(
                session_id=self.id,
                question_id=question.id,
                topic=question.topic,
                difficulty=question.difficulty,
                question_type=question.type,
            ),
            hint_level=self._learner.initial_hint_level(question.topic),
        )

    # ---- 参照系 -------------------------------------------------------

    @property
    def preparing(self) -> bool:
        return self._preparing

    @property
    def finished(self) -> bool:
        return not self._preparing and self._index >= len(self._states)

    @property
    def total(self) -> int:
        if self._preparing:
            return max(TOTAL_QUESTIONS, len(self._states))
        return len(self._states)

    def current(self) -> QuestionState:
        if self._index >= len(self._states):
            if self._preparing:
                raise IndexError("問題を準備中です")
            raise IndexError("全問終了しています")
        return self._states[self._index]

    def current_public(self) -> dict | None:
        """UI に返す現在の問題（模範解答なし）。終了後・準備中は None。"""
        if self._index >= len(self._states):
            return None
        state = self.current()
        view = state.question.public_view()
        view["number"] = self._index + 1
        view["total"] = self.total
        view["hint_level"] = state.hint_level
        return view

    # ---- 操作系 -------------------------------------------------------

    def submit_answer(self, answer: str) -> AnswerResult:
        return _consume(self.submit_answer_stream(answer))

    def submit_answer_stream(self, answer: str) -> Iterator[tuple[str, object]]:
        """判定→(正解なら)解説 を逐次イベントで yield する半二重ストリーム。

        イベント（UI はこの順で受け取る）:
          ("judgement_partial", {"reason": str})       — 判定理由の途中経過
          ("judgement", {"judgement": Judgement,
                          "question_done": bool,
                          "model_answer": str | None})  — 判定の確定
          ("explanation_partial", {"explanation": str}) — 解説の途中経過
          ("result", AnswerResult)                      — 最終結果（必ず最後）
        """
        state = self.current()
        judgement: Judgement | None = None
        for name, payload in judge_answer_stream(
            self._llm, state.question, answer, already_matched=state.matched_points
        ):
            if name == "judgement":
                judgement = payload  # type: ignore[assignment]
            else:
                yield (name, payload)
        assert judgement is not None
        state.matched_points = list(judgement.matched_points)

        state.interaction.attempts += 1
        if state.interaction.first_verdict is None:
            state.interaction.first_verdict = judgement.verdict
        state.interaction.final_verdict = judgement.verdict

        done = judgement.verdict == "correct"
        yield (
            "judgement",
            {
                "judgement": judgement,
                "question_done": done,
                "model_answer": state.question.model_answer if done else None,
            },
        )
        if not done:
            yield ("result", AnswerResult(judgement=judgement))
            return

        explanation: Explanation | None = None
        for name, payload in self._finish_question_stream(state):
            if name == "explanation":
                explanation = payload  # type: ignore[assignment]
            else:
                yield (name, payload)
        yield (
            "result",
            AnswerResult(
                judgement=judgement,
                explanation=explanation,
                question_done=True,
                model_answer=state.question.model_answer,
            ),
        )

    def request_hint(self) -> Hint:
        # ヒントは全文が出そろってから答え漏洩チェックを通す必要があるため、
        # 途中経過を UI に流せない（ストリーミング非対応のまま）
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
        return _consume(self.give_up_stream())

    def give_up_stream(self) -> Iterator[tuple[str, object]]:
        """ギブアップ処理。イベント仕様は submit_answer_stream と同じ。"""
        state = self.current()
        state.interaction.gave_up = True
        if state.interaction.first_verdict is None:
            state.interaction.first_verdict = "incorrect"
        state.interaction.final_verdict = "incorrect"
        judgement = Judgement(verdict="incorrect", reason="ギブアップ")
        yield (
            "judgement",
            {
                "judgement": judgement,
                "question_done": True,
                "model_answer": state.question.model_answer,
            },
        )
        explanation: Explanation | None = None
        for name, payload in self._finish_question_stream(state):
            if name == "explanation":
                explanation = payload  # type: ignore[assignment]
            else:
                yield (name, payload)
        yield (
            "result",
            AnswerResult(
                judgement=judgement,
                explanation=explanation,
                question_done=True,
                model_answer=state.question.model_answer,
            ),
        )

    def abort(self) -> None:
        """パネルを閉じる等の明示的な中断。コミットは中止される。"""
        if self.status == "in_progress":
            self.status = "aborted"

    # ---- 内部 ---------------------------------------------------------

    def _finish_question_stream(
        self, state: QuestionState
    ) -> Iterator[tuple[str, object]]:
        """解説を逐次 yield しつつ問題を閉じる。最後は ("explanation", Explanation)。"""
        explanation: Explanation | None = None
        groundedness_score: float | None = None
        for name, payload in generate_explanation_stream(
            self._llm, self._kb, state.question
        ):
            if name == "explanation":
                explanation, groundedness_score = payload  # type: ignore[misc]
            else:
                yield (name, payload)
        assert explanation is not None
        state.interaction.groundedness = groundedness_score
        state.done = True
        if self._history_store is not None:
            self._history_store.append(state.interaction)
        self._index += 1
        if self.finished:
            self.status = "completed"
        yield ("explanation", explanation)

    # ---- レポート -----------------------------------------------------

    def interactions(self) -> list[Interaction]:
        return [s.interaction for s in self._states if s.done]

    def report(self):
        from .evaluator import build_report

        return build_report(self.interactions(), completed=self.status == "completed")
