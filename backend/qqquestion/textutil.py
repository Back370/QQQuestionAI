"""表記ゆれ正規化と答え漏洩チェックの共通ユーティリティ。"""

from __future__ import annotations

import re
import unicodedata

_PUNCT_RE = re.compile(r"[\s、。，．,.!?！？「」『』()（）\[\]{}\"':;・=+\-*/]+")


def normalize(text: str) -> str:
    """NFKC 正規化 + 小文字化 + 空白/記号除去。"""
    return _PUNCT_RE.sub("", unicodedata.normalize("NFKC", text).lower())


def contains_answer(text: str, answers: list[str], min_len: int = 4) -> bool:
    """text に answers のいずれかが（正規化後の包含で）漏れていないか。

    min_len 未満の短い解答は誤検知が多いので照合対象にしない。
    """
    normalized_text = normalize(text)
    for answer in answers:
        normalized_answer = normalize(answer)
        if len(normalized_answer) >= min_len and normalized_answer in normalized_text:
            return True
    return False
