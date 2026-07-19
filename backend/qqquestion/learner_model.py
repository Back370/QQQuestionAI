"""学習者モデル（architecture.md §6）。

- 全対話ログを data/history.jsonl に追記（JSON Lines）
- トピック別正答率を集計し、ルールベースで出題・ヒントに反映する:
  - 正答率 50% 未満のトピック = 苦手 → 次回優先出題 + 難易度を下げる（推奨1）
  - 正答率 70% 超のトピック → 難易度を上げる（推奨2）
  - ヒント開始レベル: 苦手トピックは Lv2、それ以外は Lv1
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .models import Interaction

WEAK_THRESHOLD = 0.5
DIFFICULTY_UP_THRESHOLD = 0.7


class HistoryStore:
    def __init__(self, path: str | Path):
        self._path = Path(path)

    def append(self, interaction: Interaction) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as f:
            f.write(interaction.model_dump_json() + "\n")

    def load(self) -> list[Interaction]:
        if not self._path.exists():
            return []
        interactions = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    interactions.append(Interaction.model_validate_json(line))
                except ValueError:
                    continue  # 壊れた行は読み飛ばす（追記式ログの堅牢性優先）
        return interactions


@dataclass
class LearnerState:
    topic_scores: dict[str, float] = field(default_factory=dict)
    current_hint_level: int = 1
    attempt_count: int = 0
    history: list[Interaction] = field(default_factory=list)

    @classmethod
    def from_history(cls, history: list[Interaction]) -> "LearnerState":
        by_topic: dict[str, list[bool]] = {}
        for interaction in history:
            by_topic.setdefault(interaction.topic, []).append(interaction.final_correct)
        scores = {
            topic: sum(results) / len(results) for topic, results in by_topic.items()
        }
        return cls(topic_scores=scores, attempt_count=len(history), history=history)

    def weak_topics(self) -> list[str]:
        """正答率が低い順に返す。"""
        weak = [
            (score, topic)
            for topic, score in self.topic_scores.items()
            if score < WEAK_THRESHOLD
        ]
        weak.sort()
        return [topic for _, topic in weak]

    def initial_hint_level(self, topic: str) -> int:
        """当該トピックの正答率が高い学習者は Lv1、低い学習者は Lv2 から。"""
        score = self.topic_scores.get(topic)
        if score is not None and score < WEAK_THRESHOLD:
            return 2
        return 1

    def difficulty_bias(self) -> dict[str, int]:
        """トピック別に推奨難易度を返す（1〜3）。

        正答率が高いトピックは難易度を上げ（2）、苦手トピック（正答率が
        WEAK_THRESHOLD 未満）は難易度を下げる（1）。中間のトピックは
        既定に委ねるためエントリを出さない（プロンプトを膨らませない）。
        """
        bias: dict[str, int] = {}
        for topic, score in self.topic_scores.items():
            if score > DIFFICULTY_UP_THRESHOLD:
                bias[topic] = 2  # 正答率が高い → 難易度を上げる
            elif score < WEAK_THRESHOLD:
                bias[topic] = 1  # 苦手 → 難易度を下げる
        return bias


def load_learner_state(history_path: str | Path) -> LearnerState:
    return LearnerState.from_history(HistoryStore(history_path).load())
