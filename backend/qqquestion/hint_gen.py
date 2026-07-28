"""段階ヒント生成（architecture.md §5.2 (c), §6）。

- ヒントレベル 1〜4 で抽象度を制御
- 知識ベースの引用チャンクを根拠として渡し citations を必須化
- 生成後に答え漏洩チェック。漏洩していたら最大3回まで再生成し、
  漏洩回数（再生成前の値）を評価指標用に返す
- 部分正解のときは欠けている要点だけを対象にする（満たせている要点への
  ヒントを出し直すと、学習者は既に答えた話を繰り返すことになる）
"""

from __future__ import annotations

from typing import Sequence

from .knowledge_base import KnowledgeBase
from .llm import StructuredLLM
from .models import Hint, Question
from .question_gen import PERSONA
from .textutil import contains_answer

MAX_REGENERATIONS = 3

HINT_LEVEL_GUIDES = {
    1: "概念・分野レベルの手がかりだけを与える（例:「これは逆伝播の依存関係の話です」）。",
    2: "関連する概念・処理との対比で考えさせる（例:「順伝播のループと何が違うか比べてみましょう」）。",
    3: "コード上の着眼点を示す（どの変数のどの添字・どの行に注目すべきか）。答えそのものは言わない。",
    4: "正解を含む3つの選択肢を提示する。どれが正解かは言わない。",
}

_SYSTEM = PERSONA + """
学習者が不正解だった問題に対して、指定レベルのヒントを1つ生成してください。

絶対のルール:
- 模範解答・正解そのものを言わない（レベル4の選択肢提示を除き、正解の語を出さない）
- 与えられた参考資料(チャンク)に存在しない事実を主張しない
- 参考にしたチャンクの出典URLを citations に入れる（使わなかった場合は空でよい）
- 日本語で、教師らしく励ましながら簡潔に
- 「既に満たせている要点」が与えられた場合、その要点へのヒントは一切書かない。
  学習者が自力で答えられた話をもう一度考えさせることになるため、ヒントは
  「まだ満たせていない要点」だけに向けること。問題文全体を最初から
  考え直させる言い方（「この問題を一から整理すると」等）もしない。
"""

_PARTIAL_GUIDE = """
この学習者は部分正解です。上のルールのとおり、既に満たせている要点には触れず
（確認・言い換え・復習も不要）、欠けている要点にだけ焦点を絞ってヒントを出して
ください。ヒントの導入も「〜は既に説明できているので、残りは…」のように、
欠けている観点へ直接向かわせる形にすること。
"""


def generate_hint(
    llm: StructuredLLM,
    kb: KnowledgeBase,
    question: Question,
    user_answer: str,
    hint_level: int,
    matched_points: Sequence[str] = (),
    missing_points: Sequence[str] = (),
) -> tuple[Hint, int]:
    """ヒントと「漏洩により再生成した回数」を返す。

    matched_points / missing_points は部分正解の内訳。両方が与えられたときは
    欠けている要点だけを対象にヒントを組む（満たせている要点への手がかりは
    学習者にとって既知で、出しても遠回りにしかならない）。
    """
    level = max(1, min(4, hint_level))
    matched = list(matched_points)
    missing = list(missing_points)
    # 満たせた要点と欠けた要点の両方が分かって初めて「絞り込み」が成立する
    focused = bool(matched and missing)

    # 絞り込めているなら、参考資料も欠けている要点に寄せて引く
    query = f"{question.topic} {' '.join(missing)}" if focused else f"{question.topic} {question.text}"
    chunks = kb.query(query, k=4)
    sources = "\n\n".join(
        f"[{i + 1}] {chunk.title} ({chunk.url})\n{chunk.text}"
        for i, chunk in enumerate(chunks)
    ) or "(参考資料なし。一般論の範囲でヒントを出すこと)"

    system = _SYSTEM + _PARTIAL_GUIDE if focused else _SYSTEM
    focus = ""
    if focused:
        focus = (
            f"既に満たせている要点(ヒントを出してはいけない): {matched}\n"
            f"まだ満たせていない要点(ヒントの対象。内容は漏らさない): {missing}\n"
        )

    user = (
        f"問題: {question.text}\n"
        + (f"コード:\n{question.code_snippet}\n" if question.code_snippet else "")
        + f"学習者の解答: {user_answer}\n"
        + focus
        + f"模範解答(漏らしてはいけない): {question.model_answer}\n"
        f"ヒントレベル {level}: {HINT_LEVEL_GUIDES[level]}\n\n"
        f"参考資料:\n{sources}"
    )

    # 既に満たせている要点は学習者自身が書いた内容なので、ヒントに現れても
    # 漏洩ではない（judge の reason と同じ扱い）。漏洩チェックの対象外にする
    skip = set(matched) if focused else set()
    forbidden = [
        question.model_answer,
        *(p for p in question.accepted_points if p not in skip),
    ]
    leaks = 0
    hint = llm.generate(Hint, system, user, temperature=0.3)
    # レベル4は選択肢に正解を含むため漏洩チェックの対象外
    while level < 4 and contains_answer(hint.hint, forbidden) and leaks < MAX_REGENERATIONS:
        leaks += 1
        hint = llm.generate(
            Hint,
            system + "\n前回の生成は正解を漏らしていました。正解の語を含めずに言い直すこと。",
            user,
            temperature=0.3,
        )
    return hint, leaks
