"""全モジュール共通のデータモデル（architecture.md §5 の IO 仕様に対応）。"""

from __future__ import annotations

import time
from typing import Literal

from pydantic import BaseModel, Field

QuestionType = Literal["prerequisite", "implementation"]
Verdict = Literal["correct", "partial", "incorrect"]


class Question(BaseModel):
    id: str
    type: QuestionType
    text: str
    code_snippet: str | None = None
    model_answer: str
    accepted_points: list[str] = Field(default_factory=list)
    rubric: str
    topic: str
    difficulty: int = 1

    def public_view(self) -> dict:
        """UI へ返してよい部分だけ。模範解答・採点基準は絶対に含めない。"""
        return {
            "id": self.id,
            "type": self.type,
            "text": self.text,
            "code_snippet": self.code_snippet,
            "topic": self.topic,
            "difficulty": self.difficulty,
        }


class QuestionSet(BaseModel):
    questions: list[Question]


class Judgement(BaseModel):
    verdict: Verdict
    matched_points: list[str] = Field(default_factory=list)
    missing_points: list[str] = Field(default_factory=list)
    reason: str = ""


class Hint(BaseModel):
    hint: str
    citations: list[str] = Field(default_factory=list)


class Explanation(BaseModel):
    explanation: str
    citations: list[str] = Field(default_factory=list)


class Chunk(BaseModel):
    """知識ベースの1チャンク。"""

    text: str
    url: str = ""
    title: str = ""
    topic: str = ""


class DiffContext(BaseModel):
    """diff_analyzer の出力。"""

    diff_text: str
    files: list[str] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)


class Interaction(BaseModel):
    """1問ぶんの対話記録（data/history.jsonl に追記される単位）。"""

    session_id: str
    question_id: str
    topic: str
    difficulty: int = 1
    question_type: QuestionType = "prerequisite"
    first_verdict: Verdict | None = None
    final_verdict: Verdict | None = None
    attempts: int = 0
    hints_shown: int = 0
    max_hint_level: int = 0
    leaked_hint_regenerations: int = 0
    gave_up: bool = False
    groundedness: float | None = None
    timestamp: float = Field(default_factory=time.time)

    @property
    def first_correct(self) -> bool:
        return self.first_verdict == "correct"

    @property
    def final_correct(self) -> bool:
        return self.final_verdict == "correct"
