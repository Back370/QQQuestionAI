"""コアループ検証用 CLI（architecture.md §8 実装順序 1）。

Webview を使わずターミナルで 出題→解答→判定→ヒント→解説→レポート
を回す。「ヒント」でヒント要求、「ギブアップ」で降参。

例:
    GOOGLE_API_KEY=... python -m qqquestion.cli --repo /path/to/repo
    QQQ_FAKE_LLM=1 python -m qqquestion.cli --demo   # APIキー不要のデモ
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import diff_analyzer
from .knowledge_base import (
    KnowledgeBaseBuilder,
    create_knowledge_base,
    create_search_provider,
)
from .learner_model import HistoryStore, load_learner_state
from .llm import LLMUnavailableError, create_llm
from .session import QuizSession
from .terminput import enable_line_editing

BANNER = """\
==========================================================
  QQQuestionAI 「答えは教えません。でも必ず説明できるようになります」
==========================================================
記述式で解答してください。困ったら「ヒント」、降参は「ギブアップ」。
"""


def run(repo_path: str, data_dir: str, diff_file: str | None, demo: bool) -> int:
    enable_line_editing()  # input() を日本語（マルチバイト）でも1文字ずつ削除できるようにする
    if demo:
        import os

        os.environ["QQQ_FAKE_LLM"] = "1"

    if diff_file:
        diff_ctx = diff_analyzer.analyze(Path(diff_file).read_text(encoding="utf-8"))
    else:
        diff_ctx = diff_analyzer.analyze_staged(repo_path)
    if not diff_ctx.diff_text.strip():
        print("ステージ済みの差分がありません（git add してから実行してください）")
        return 1

    data = Path(data_dir)
    data.mkdir(parents=True, exist_ok=True)
    kb = create_knowledge_base(str(data))
    builder = KnowledgeBaseBuilder(kb, create_search_provider(), data / "kb_cache.json")

    print(BANNER)
    print(f"対象差分: {', '.join(diff_ctx.files) or '(不明)'}")
    print(f"抽出トピック: {' / '.join(diff_ctx.topics) or '(なし)'}")
    added = builder.build_for_topics(diff_ctx.topics)
    print(f"知識ベース: {kb.count()} チャンク (新規 {added})\n")

    learner_state = load_learner_state(data / "history.jsonl")
    if learner_state.weak_topics():
        print(f"前回の苦手傾向: {' / '.join(learner_state.weak_topics())} → 優先出題します\n")

    try:
        session = QuizSession(
            llm=create_llm(),
            kb=kb,
            diff_ctx=diff_ctx,
            learner_state=learner_state,
            history_store=HistoryStore(data / "history.jsonl"),
            origin="cli",
        )
    except LLMUnavailableError as error:
        print(f"\n出題を生成できませんでした: {error}")
        return 1

    while not session.finished:
        view = session.current_public()
        assert view is not None
        type_label = "前提知識" if view["type"] == "prerequisite" else "実装の説明"
        print(f"【第{view['number']}問/{view['total']}】({type_label}・難易度{view['difficulty']})")
        print(view["text"])
        if view["code_snippet"]:
            print()
            for line in view["code_snippet"].splitlines():
                print(f"    {line}")
        print()

        while True:
            try:
                user_input = input("あなた> ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n中断しました。（コミットには影響しません）")
                session.abort()
                print(session.report().render())
                return 1

            if not user_input:
                continue
            if user_input in ("ヒント", "hint"):
                hint = session.request_hint()
                state = session.current()
                print(f"先生(ヒントLv{state.interaction.max_hint_level})> {hint.hint}")
                for url in hint.citations:
                    print(f"  出典: {url}")
                continue
            if user_input in ("ギブアップ", "giveup"):
                _consume_stream(session.give_up_stream())
                break

            result = _consume_stream(session.submit_answer_stream(user_input))
            if result.judgement.verdict == "correct":
                break
        print()

    print(session.report().render())
    print("理解度チェック完走。おつかれさまでした。")
    return 0


class _TypeWriter:
    """スナップショット（毎回全文）を受け取り、増分だけを逐次表示する。"""

    def __init__(self):
        self._shown = ""

    @property
    def started(self) -> bool:
        return bool(self._shown)

    def update(self, text: str) -> None:
        if text.startswith(self._shown):
            delta = text[len(self._shown):]
        else:
            delta = "\n" + text  # フォールバック等で全文が差し替わった場合
        if delta:
            print(delta, end="", flush=True)
            self._shown = text


def _consume_stream(events):
    """セッションのストリームを逐次表示しながら最終結果を返す（半二重）。"""
    reason = _TypeWriter()
    explanation = _TypeWriter()
    result = None
    for name, payload in events:
        if name == "judgement_partial":
            if not reason.started:
                print("先生> ", end="", flush=True)
            reason.update(payload["reason"])
        elif name == "judgement":
            if reason.started:
                print()
            _print_verdict(payload, streamed_reason=reason.started)
        elif name == "explanation_partial":
            if not explanation.started:
                print("\n----- 解説 -----")
            explanation.update(payload["explanation"])
        elif name == "result":
            result = payload
    if explanation.started:
        print()
        if result is not None and result.explanation and result.explanation.citations:
            print("出典:")
            for url in result.explanation.citations:
                print(f"  - {url}")
        print("----------------")
    return result


def _print_verdict(payload, streamed_reason: bool) -> None:
    judgement = payload["judgement"]
    if judgement.verdict == "correct":
        suffix = "" if streamed_reason else f" ({judgement.reason})"
        print(f"先生> 正解です！🎉{suffix}")
    elif judgement.verdict == "partial":
        middle = "" if streamed_reason else f"{judgement.reason} "
        print(
            f"先生> 部分的に正解です。{middle}"
            "正解済みの部分は繰り返さなくてよいので、足りない部分だけ補足してください。"
        )
    elif payload["question_done"]:  # ギブアップ
        print(f"\n正解は「{payload['model_answer']}」でした。")
    else:
        print("先生> 残念、違います。「ヒント」と言ってくれれば手がかりを出しますよ。")


def main() -> None:
    from .envfile import load_env_file

    load_env_file()  # backend/.env から GOOGLE_API_KEY 等を読み込む（任意）
    parser = argparse.ArgumentParser(description="QQQuestionAI 理解度チェック CLI")
    parser.add_argument("--repo", default=".", help="対象リポジトリ")
    parser.add_argument(
        "--data-dir",
        # server.py と揃える。拡張が生成する shim は書き込み可能な場所を渡す
        default=os.environ.get("QQQ_DATA_DIR", "data"),
        help="履歴・知識ベースの保存先 (既定: $QQQ_DATA_DIR or data)",
    )
    parser.add_argument("--diff-file", default=None, help="差分ファイルから出題（デバッグ用）")
    parser.add_argument("--demo", action="store_true", help="APIキー不要のデモモード")
    args = parser.parse_args()
    sys.exit(run(args.repo, args.data_dir, args.diff_file, args.demo))


if __name__ == "__main__":
    main()
