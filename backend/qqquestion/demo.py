"""QQQ_FAKE_LLM=1 用のオフラインデモ実装。

API キーなしで全フロー（出題→判定→ヒント→解説→レポート）を
動かすための決定的な FakeLLM を組み立てる。出題は direction.md の
RNN 教材5問の缶詰。判定は accepted_points とのトークン重なりによる
ルールベース近似。
"""

from __future__ import annotations

import ast
import re

from .llm import FakeLLM
from .models import Explanation, Hint, Judgement, Question, QuestionSet
from .textutil import normalize

DEMO_QUESTIONS = [
    Question(
        id="q1",
        type="prerequisite",
        text=(
            "RNN（リカレントニューラルネットワーク）が、通常の全結合ニューラル"
            "ネットワークと決定的に違う点は何ですか。「再帰結合」という語を使って"
            "説明してください。"
        ),
        model_answer=(
            "隠れ層が再帰結合を持ち、前の時刻の隠れ状態を次の時刻の入力として"
            "使うことで系列の文脈を保持できる点。"
        ),
        accepted_points=["再帰結合", "前の時刻の隠れ状態", "系列・文脈の保持"],
        rubric="再帰結合と前時刻状態の利用の両方に言及すれば correct、片方なら partial。",
        topic="RNN",
        difficulty=1,
    ),
    Question(
        id="q2",
        type="prerequisite",
        text=(
            "この実装で誤差関数として使われているクロスエントロピーは、"
            "何と何の間の何を測る関数ですか。"
        ),
        model_answer=(
            "softmaxが出力する予測確率分布と、正解ラベルのone-hot分布の間の"
            "隔たり（近さ）を測る関数。"
        ),
        accepted_points=["予測確率分布", "正解のone-hot分布", "分布の間の隔たり・距離"],
        rubric="「予測分布」「正解分布」「その間の距離」の3要素が揃えば correct。",
        topic="クロスエントロピー",
        difficulty=1,
    ),
    Question(
        id="q3",
        type="implementation",
        text="次の部分は何をしているか説明してください。",
        code_snippet=(
            "Z_prime = np.zeros((q, T+1))\n\n"
            "for t in range(T):\n"
            "    Z_prime[:, t+1], nabla_f[:, t] = forward(np.append(1, xi[t,:]),"
            " Z_prime[:, t], W_in, W, sigmoid)"
        ),
        model_answer=(
            "時刻0からT-1まで順伝播を回し、各時刻の隠れ状態をZ_primeの列に、"
            "活性化関数の勾配をnabla_fに保存している。初期状態はゼロベクトル。"
        ),
        accepted_points=["順伝播", "隠れ状態の保存", "活性化関数の勾配の保存"],
        rubric="順伝播であること＋2つの保存対象に触れれば correct、順伝播のみは partial。",
        topic="RNN",
        difficulty=2,
    ),
    Question(
        id="q4",
        type="implementation",
        text="逆伝播のループで、なぜ reversed（時刻の逆順）で回す必要があるのですか。",
        code_snippet=(
            "for t in reversed(range(T)):\n"
            "    if t == T-1:\n"
            "        delta[:, t] = backward(W, W_out[:, 1:], np.zeros(q), delta_out,"
            " nabla_f[:, t])\n"
            "    else:\n"
            "        delta[:, t] = backward(W, W_out[:, 1:], delta[:, t+1], np.zeros(m),"
            " nabla_f[:, t])"
        ),
        model_answer=(
            "delta[:, t] の計算に未来の delta[:, t+1] が必要で、時刻tの誤差が"
            "未来の誤差に依存するため、T-1から過去へ順に計算する必要がある。"
        ),
        accepted_points=["delta[t+1]への依存", "未来から過去への計算順序"],
        rubric="t+1への依存関係が言えれば correct、「逆から回す」だけでは incorrect。",
        topic="誤差逆伝播",
        difficulty=3,
    ),
    Question(
        id="q5",
        type="implementation",
        text=(
            "dEdW（再帰重み W の勾配）の計算で、Z_prime の最後の列を除いた"
            " Z_prime[:, :T] を使っているのはなぜですか。"
        ),
        code_snippet="dEdW = np.dot(delta, Z_prime[:, :T].T)",
        model_answer=(
            "Wは時刻t-1の隠れ状態を時刻tへ伝える重みなので、勾配は各時刻の"
            "deltaと1つ前の時刻の隠れ状態の積で決まる。Z_prime[:, :T]は"
            "各deltaに対応する1時刻前の状態だから。"
        ),
        accepted_points=["Wは前時刻の状態を運ぶ重み", "deltaと1時刻前の状態の対応"],
        rubric="「1時刻前の状態との対応」が言えれば correct、形合わせのみは incorrect。",
        topic="勾配計算",
        difficulty=3,
    ),
]

_POINTS_RE = re.compile(r"要点\(accepted_points\): (\[.*?\])\n", re.DOTALL)
_ANSWER_RE = re.compile(r"学習者の解答: (.*)\Z", re.DOTALL)


def _judge_by_rule(system: str, user: str) -> Judgement:
    """accepted_points との正規化トークン照合による決定的判定。"""
    points_match = _POINTS_RE.search(user)
    answer_match = _ANSWER_RE.search(user)
    points: list[str] = (
        ast.literal_eval(points_match.group(1)) if points_match else []
    )
    answer = normalize(answer_match.group(1)) if answer_match else ""

    matched, missing = [], []
    for point in points:
        # 要点の内容語（英語3文字以上 / 漢字2文字以上 / カタカナ3文字以上）が
        # 1つでも解答に現れれば充足とみなす。ひらがな混じりの長いフレーズを
        # 丸ごと1トークンにすると照合が厳しすぎるため、内容語単位で切る
        tokens = re.findall(r"[A-Za-z_]{3,}|[一-鿿]{2,}|[ァ-ヶー]{3,}", point)
        if tokens and any(normalize(token) in answer for token in tokens):
            matched.append(point)
        else:
            missing.append(point)

    if points and not missing:
        verdict = "correct"
    elif matched:
        verdict = "partial"
    else:
        verdict = "incorrect"
    return Judgement(
        verdict=verdict,
        matched_points=matched,
        missing_points=missing,
        reason=f"要点 {len(matched)}/{len(points)} を満たしています（デモ判定）。",
    )


_LEVEL_RE = re.compile(r"ヒントレベル (\d)")
_MODEL_ANSWER_RE = re.compile(r"模範解答(?:\(漏らしてはいけない\))?: (.*)")


def _hint_by_rule(system: str, user: str) -> Hint:
    level_match = _LEVEL_RE.search(user)
    level = int(level_match.group(1)) if level_match else 1
    texts = {
        1: "まず、この処理がどの概念に関係するかを大きく捉えてみましょう。",
        2: "似た処理と何が違うのか、対比で考えてみましょう。",
        3: "コードの添字（tとt+1、列の範囲）に注目してみてください。",
        4: "3つの選択肢から選んでみましょう: (A) 形を合わせるため (B) 1時刻前の状態と対応させるため (C) 計算量削減のため",
    }
    return Hint(
        hint=f"(デモLv{level}) {texts.get(level, texts[1])}",
        citations=["https://example.com/qqquestion-demo-kb"],
    )


def _explain_by_rule(system: str, user: str) -> Explanation:
    answer_match = _MODEL_ANSWER_RE.search(user)
    model_answer = answer_match.group(1) if answer_match else ""
    return Explanation(
        explanation=(
            f"ポイントはこうです: {model_answer} "
            "コードの添字が「どの時刻のデータか」を常に意識するのが覚えるコツです。"
        ),
        citations=["https://example.com/qqquestion-demo-kb"],
    )


def _question_set_by_rule(system: str, user: str) -> QuestionSet:
    # 「残り4問」の要求（第1問は出題済み）なら q2〜q5 を返す
    if "出題済み" in user:
        return QuestionSet(questions=list(DEMO_QUESTIONS[1:]))
    return QuestionSet(questions=list(DEMO_QUESTIONS))


def build_demo_llm() -> FakeLLM:
    llm = FakeLLM()
    llm.set_default(QuestionSet, _question_set_by_rule)
    # 第1問の先行生成（Question スキーマ単体）用
    llm.set_default(Question, lambda system, user: DEMO_QUESTIONS[0].model_copy())
    llm.set_default(Judgement, _judge_by_rule)
    llm.set_default(Hint, _hint_by_rule)
    llm.set_default(Explanation, _explain_by_rule)
    return llm
