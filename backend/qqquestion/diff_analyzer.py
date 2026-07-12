"""ステージ済み差分の取得とトピック抽出（architecture.md §3 データフロー 2）。

トピック抽出はルールベース: キーワード辞書とのマッチ + 変更された
関数/クラス名。LLM に委ねないので決定的でテスト可能。
"""

from __future__ import annotations

import re
import subprocess

from .models import DiffContext

# 差分中の語 → トピック名（小文字で照合）
KEYWORD_TOPICS: dict[str, str] = {
    "rnn": "RNN",
    "recurrent": "RNN",
    "lstm": "LSTM",
    "backward": "誤差逆伝播",
    "backprop": "誤差逆伝播",
    "bptt": "BPTT",
    "delta": "誤差逆伝播",
    "softmax": "softmax",
    "crossentropy": "クロスエントロピー",
    "cross_entropy": "クロスエントロピー",
    "entoropy": "クロスエントロピー",  # 教材コードの綴りゆれに対応
    "adam": "Adam最適化",
    "sgd": "確率的勾配降下法",
    "gradient": "勾配計算",
    "dedw": "勾配計算",
    "np.outer": "行列演算(numpy)",
    "np.dot": "行列演算(numpy)",
    "sigmoid": "活性化関数",
    "relu": "活性化関数",
    "embedding": "埋め込み表現",
    "attention": "Attention機構",
    "transformer": "Transformer",
    "async": "非同期処理",
    "await": "非同期処理",
    "thread": "並行処理",
    "mutex": "排他制御",
    "lock": "排他制御",
    "regex": "正規表現",
    "sql": "SQL",
    "index": "インデックス",
    "cache": "キャッシュ",
    "recursion": "再帰",
    "recursive": "再帰",
}

_FILE_RE = re.compile(r"^\+\+\+ b/(.+)$", re.MULTILINE)
_DEF_RE = re.compile(r"^\+\s*(?:def|class)\s+([A-Za-z_][A-Za-z0-9_]*)", re.MULTILINE)


def get_staged_diff(repo_path: str = ".") -> str:
    """コミット対象（ステージ済み）の差分だけを取得する。"""
    result = subprocess.run(
        ["git", "diff", "--cached", "--no-color"],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def extract_topics(diff_text: str, max_topics: int = 6) -> list[str]:
    """追加行と変更ファイル名のキーワードからトピックを抽出する（出現順・重複なし）。"""
    added_lines = "\n".join(
        line for line in diff_text.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    ).lower()
    # ファイル名も手がかりになる（例: rnn_train.py → RNN）
    added_lines += "\n" + "\n".join(_FILE_RE.findall(diff_text)).lower()

    topics: list[str] = []
    for keyword, topic in KEYWORD_TOPICS.items():
        if keyword in added_lines and topic not in topics:
            topics.append(topic)

    # キーワードにかからない差分でも、定義された関数/クラス名は手がかりになる
    if not topics:
        for name in _DEF_RE.findall(diff_text)[:3]:
            topic = name.replace("_", " ")
            if topic not in topics:
                topics.append(topic)

    return topics[:max_topics]


def analyze(diff_text: str) -> DiffContext:
    files = _FILE_RE.findall(diff_text)
    return DiffContext(
        diff_text=diff_text,
        files=files,
        topics=extract_topics(diff_text),
    )


def analyze_staged(repo_path: str = ".") -> DiffContext:
    return analyze(get_staged_diff(repo_path))
