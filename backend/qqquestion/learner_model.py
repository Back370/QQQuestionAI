"""学習者モデル（architecture.md §6）。

- 全対話ログを data/history.jsonl に追記（JSON Lines）
- トピック別正答率を集計し、ルールベースで出題・ヒントに反映する:
  - 正答率は**トピックごとに直近 RECENT_WINDOW 件**で見る（古い不正解を引きずらない）
  - 正答率 50% 未満のトピック = 苦手 → 難易度を下げる（推奨1）
  - 正答率 70% 超のトピック → 難易度を上げる（推奨2）
  - ヒント開始レベル: 苦手トピックは Lv2、それ以外は Lv1
  - 苦手のうち**今回の差分に関係するもの**だけを優先出題に回す（priority_topics）
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .models import Interaction
from .textutil import normalize

WEAK_THRESHOLD = 0.5
DIFFICULTY_UP_THRESHOLD = 0.7
# トピックごとに何件の履歴で正答率を見るか。全履歴で平均すると、一度の不正解が
# 何セッションも残り、あとから正解できるようになっても「苦手」から抜けられない
# （克服が反映されない）。直近だけを見ることで、正解の積み上げが苦手判定を外す。
RECENT_WINDOW = 5
# 出題プロンプトに載せる優先出題トピックの上限。多く並べるほど1問あたりの
# 制約が増え、差分から離れた出題や指示の丸ごと無視が起きる。
MAX_PRIORITY_TOPICS = 3
# 「克服した」と見なすのに必要な直近の解答数（1件だけの正解では判定しない）
MIN_ATTEMPTS_FOR_OVERCOME = 2


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


def is_related_topic(topic: str, diff_topics: list[str]) -> bool:
    """トピック名が今回の差分のトピック候補に対応するか（正規化した包含で照合）。

    履歴側のトピック名は LLM が付けるため、差分側の語彙（diff_analyzer の
    KEYWORD_TOPICS）と完全一致しない（例: 「排他制御(Lock)」「非同期処理/async」）。
    そのため双方向の部分一致で見る。
    """
    normalized = normalize(topic)
    if not normalized:
        return False
    for diff_topic in diff_topics:
        other = normalize(diff_topic)
        if other and (normalized in other or other in normalized):
            return True
    return False


@dataclass
class LearnerState:
    topic_scores: dict[str, float] = field(default_factory=dict)
    # 直近ウィンドウ内の解答数（少ない実績で難易度・克服を判定しないために持つ）
    topic_attempts: dict[str, int] = field(default_factory=dict)
    # 過去に一度でも不正解だったトピック（克服の判定に使う）
    stumbled_topics: set[str] = field(default_factory=set)
    current_hint_level: int = 1
    attempt_count: int = 0
    history: list[Interaction] = field(default_factory=list)

    @classmethod
    def from_history(cls, history: list[Interaction]) -> "LearnerState":
        """履歴（追記順＝時系列）からトピック別の直近正答率を集計する。"""
        by_topic: dict[str, list[bool]] = {}
        for interaction in history:
            by_topic.setdefault(interaction.topic, []).append(interaction.final_correct)
        scores: dict[str, float] = {}
        attempts: dict[str, int] = {}
        stumbled: set[str] = set()
        for topic, results in by_topic.items():
            recent = results[-RECENT_WINDOW:]  # 直近だけで判定する（古い不正解を捨てる）
            scores[topic] = sum(recent) / len(recent)
            attempts[topic] = len(recent)
            if not all(results):
                stumbled.add(topic)
        return cls(
            topic_scores=scores,
            topic_attempts=attempts,
            stumbled_topics=stumbled,
            attempt_count=len(history),
            history=history,
        )

    def weak_topics(self) -> list[str]:
        """苦手トピックを正答率が低い順に返す（直近 RECENT_WINDOW 件で判定）。"""
        weak = [
            (score, topic)
            for topic, score in self.topic_scores.items()
            if score < WEAK_THRESHOLD
        ]
        weak.sort()
        return [topic for _, topic in weak]

    def priority_topics(self, diff_topics: list[str]) -> list[str]:
        """今回のセッションで優先出題する苦手トピック（正答率が低い順）。

        苦手トピックを全部プロンプトに渡すと、今回の差分と関係のないトピック
        （別ファイル・別分野の履歴）まで「優先的に出題」と指示することになり、
        差分から離れた出題になるか、指示が丸ごと無視される。今回の差分に
        対応するものだけを MAX_PRIORITY_TOPICS 件まで渡す。
        """
        related = [
            topic for topic in self.weak_topics() if is_related_topic(topic, diff_topics)
        ]
        return related[:MAX_PRIORITY_TOPICS]

    def overcome_topics(self) -> list[str]:
        """過去につまずいたが、直近では安定して正解できているトピック。

        出題には使わない（学習者に克服の進捗を見せるための一覧）。
        """
        return sorted(
            topic
            for topic in self.stumbled_topics
            if self.topic_attempts.get(topic, 0) >= MIN_ATTEMPTS_FOR_OVERCOME
            and self.topic_scores.get(topic, 0.0) > DIFFICULTY_UP_THRESHOLD
        )

    def initial_hint_level(self, topic: str) -> int:
        """当該トピックの正答率が高い学習者は Lv1、低い学習者は Lv2 から。"""
        score = self.topic_scores.get(topic)
        if score is not None and score < WEAK_THRESHOLD:
            return 2
        return 1

    def difficulty_bias(self, diff_topics: list[str] | None = None) -> dict[str, int]:
        """トピック別に推奨難易度を返す（1〜3）。

        正答率が高いトピックは難易度を上げ（2）、苦手トピック（正答率が
        WEAK_THRESHOLD 未満）は難易度を下げる（1）。中間のトピックは
        既定に委ねるためエントリを出さない（プロンプトを膨らませない）。

        diff_topics を渡すと、今回の差分に関係するトピックだけに絞る。
        履歴が伸びるほど無関係なトピックが並び、プロンプトが薄まるため。
        """
        bias: dict[str, int] = {}
        for topic, score in self.topic_scores.items():
            if diff_topics is not None and not is_related_topic(topic, diff_topics):
                continue
            if score > DIFFICULTY_UP_THRESHOLD:
                bias[topic] = 2  # 正答率が高い → 難易度を上げる
            elif score < WEAK_THRESHOLD:
                bias[topic] = 1  # 苦手 → 難易度を下げる
        return bias

    def weak_topic_scores(self) -> dict[str, float]:
        """苦手トピックの直近正答率（表示用。UI へはこの値だけ渡す）。"""
        return {topic: self.topic_scores[topic] for topic in self.weak_topics()}


def format_learner_summary(
    weak_topics: list[str],
    priority_topics: list[str],
    overcome_topics: list[str],
    scores: dict[str, float] | None = None,
) -> list[str]:
    """苦手傾向の表示行を組み立てる（cli / remote_cli で共通）。

    「苦手 = 全部いま出題される」ではないこと（今回の差分に関係するものだけを
    優先出題すること）と、克服できたトピックが抜けていくことを明示する。
    """

    def label(topic: str) -> str:
        score = (scores or {}).get(topic)
        return topic if score is None else f"{topic}({score:.0%})"

    lines: list[str] = []
    if priority_topics:
        lines.append(
            f"苦手傾向: {' / '.join(label(t) for t in priority_topics)}"
            " → 今回の差分に関係するので優先出題します"
        )
        rest = [t for t in weak_topics if t not in priority_topics]
        if rest:
            lines.append(
                f"（今回は出題しない苦手: {' / '.join(label(t) for t in rest)}"
                " — 関係する差分のときに出題します）"
            )
    elif weak_topics:
        lines.append(
            f"苦手傾向: {' / '.join(label(t) for t in weak_topics)}"
            "（今回の差分に関係しないため優先出題しません）"
        )
    if overcome_topics:
        lines.append(
            f"克服したトピック: {' / '.join(overcome_topics)}"
            f"（直近{RECENT_WINDOW}件で正答率{DIFFICULTY_UP_THRESHOLD:.0%}超）"
        )
    return lines


def load_learner_state(history_path: str | Path) -> LearnerState:
    return LearnerState.from_history(HistoryStore(history_path).load())
