"""段階ヒント生成（architecture.md §5.2 (c), §6）。

- ヒントレベル 1〜4 で抽象度を制御
- 知識ベースの引用チャンクを根拠として渡し citations を必須化
- 生成後に答え漏洩チェック。漏洩していたら最大3回まで再生成し、
  漏洩回数（再生成前の値）を評価指標用に返す
"""

from __future__ import annotations

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
"""


def generate_hint(
    llm: StructuredLLM,
    kb: KnowledgeBase,
    question: Question,
    user_answer: str,
    hint_level: int,
) -> tuple[Hint, int]:
    """ヒントと「漏洩により再生成した回数」を返す。"""
    level = max(1, min(4, hint_level))
    chunks = kb.query(f"{question.topic} {question.text}", k=4)
    sources = "\n\n".join(
        f"[{i + 1}] {chunk.title} ({chunk.url})\n{chunk.text}"
        for i, chunk in enumerate(chunks)
    ) or "(参考資料なし。一般論の範囲でヒントを出すこと)"

    user = (
        f"問題: {question.text}\n"
        + (f"コード:\n{question.code_snippet}\n" if question.code_snippet else "")
        + f"学習者の解答(不正解): {user_answer}\n"
        f"模範解答(漏らしてはいけない): {question.model_answer}\n"
        f"ヒントレベル {level}: {HINT_LEVEL_GUIDES[level]}\n\n"
        f"参考資料:\n{sources}"
    )

    forbidden = [question.model_answer, *question.accepted_points]
    leaks = 0
    hint = llm.generate(Hint, _SYSTEM, user, temperature=0.3)
    # レベル4は選択肢に正解を含むため漏洩チェックの対象外
    while level < 4 and contains_answer(hint.hint, forbidden) and leaks < MAX_REGENERATIONS:
        leaks += 1
        hint = llm.generate(
            Hint,
            _SYSTEM + "\n前回の生成は正解を漏らしていました。正解の語を含めずに言い直すこと。",
            user,
            temperature=0.3,
        )
    return hint, leaks
