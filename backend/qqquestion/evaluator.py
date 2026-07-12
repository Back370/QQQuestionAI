"""評価指標（architecture.md §7）。

セッション終了時のレポートと、data/eval_set.json による
判定精度のオフライン評価を提供する。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .models import Interaction, Judgement, Question


@dataclass
class SessionReport:
    attempted: int
    first_correct_rate: float
    final_correct_rate: float
    hints_shown: int
    leaked_regenerations: int
    hint_effective_rate: float
    groundedness: float
    weak_topic_notes: list[str]
    completed: bool

    def render(self) -> str:
        note = " / ".join(self.weak_topic_notes) if self.weak_topic_notes else "なし"
        lines = [
            "===== セッション評価レポート =====",
            f"挑戦した問題数     : {self.attempted}" + ("" if self.completed else " (中断)"),
            f"初回正答率         : {self.first_correct_rate:.0%}",
            f"最終正答率         : {self.final_correct_rate:.0%} (ヒント後の到達度)",
            f"提示ヒント数       : {self.hints_shown}",
            f"答え漏洩率         : {self.leak_rate:.0%} (再生成で抑止済み)",
            f"ヒント有効率       : {self.hint_effective_rate:.0%}",
            f"解説の根拠被覆率   : {self.groundedness:.0%}",
            f"苦手傾向メモ       : {note}",
            "==================================",
        ]
        return "\n".join(lines)

    @property
    def leak_rate(self) -> float:
        """ヒント中に正解が含まれた割合（再生成前の値で集計）。"""
        total = self.hints_shown + self.leaked_regenerations
        return self.leaked_regenerations / total if total else 0.0


def build_report(interactions: list[Interaction], completed: bool = True) -> SessionReport:
    attempted = len(interactions)
    if attempted == 0:
        return SessionReport(0, 0.0, 0.0, 0, 0, 0.0, 0.0, [], completed)

    first_correct = sum(1 for i in interactions if i.first_correct)
    final_correct = sum(1 for i in interactions if i.final_correct)
    hints_shown = sum(i.hints_shown for i in interactions)
    leaked = sum(i.leaked_hint_regenerations for i in interactions)

    # ヒント有効率: ヒントを見た問題のうち最終的に正解へ至った割合
    hinted = [i for i in interactions if i.hints_shown > 0]
    hint_effective = (
        sum(1 for i in hinted if i.final_correct) / len(hinted) if hinted else 0.0
    )

    grounded_values = [i.groundedness for i in interactions if i.groundedness is not None]
    groundedness = sum(grounded_values) / len(grounded_values) if grounded_values else 0.0

    weak_notes = sorted(
        {i.topic for i in interactions if not i.final_correct or i.hints_shown > 0}
    )

    return SessionReport(
        attempted=attempted,
        first_correct_rate=first_correct / attempted,
        final_correct_rate=final_correct / attempted,
        hints_shown=hints_shown,
        leaked_regenerations=leaked,
        hint_effective_rate=hint_effective,
        groundedness=groundedness,
        weak_topic_notes=weak_notes,
        completed=completed,
    )


def evaluate_judge(
    judge_fn: Callable[[Question, str], Judgement], eval_set_path: str | Path
) -> dict:
    """data/eval_set.json で判定精度をオフライン評価する。

    eval_set.json の形式:
    [{"question": {...Question...}, "answer": "...", "expected": "correct|partial|incorrect"}]
    """
    cases = json.loads(Path(eval_set_path).read_text(encoding="utf-8"))
    total = len(cases)
    correct = 0
    failures = []
    for case in cases:
        question = Question.model_validate(case["question"])
        judgement = judge_fn(question, case["answer"])
        if judgement.verdict == case["expected"]:
            correct += 1
        else:
            failures.append(
                {
                    "question_id": question.id,
                    "answer": case["answer"],
                    "expected": case["expected"],
                    "actual": judgement.verdict,
                }
            )
    return {
        "total": total,
        "correct": correct,
        "accuracy": correct / total if total else 0.0,
        "failures": failures,
    }


def main() -> None:
    """判定精度のオフライン評価を実行する。

    使い方: python -m qqquestion.evaluator [eval_set.json のパス]
    （QQQ_FAKE_LLM=1 でデモ判定、GOOGLE_API_KEY があれば実LLMで評価）
    """
    import sys

    from .envfile import load_env_file
    from .judge import judge_answer
    from .llm import create_llm

    load_env_file()
    path = sys.argv[1] if len(sys.argv) > 1 else "data/eval_set.json"
    llm = create_llm()
    result = evaluate_judge(lambda q, a: judge_answer(llm, q, a), path)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
