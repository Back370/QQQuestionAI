"""コアループ検証用 CLI（architecture.md §8 実装順序 1）。

Webview を使わずターミナルで 出題→解答→判定→ヒント→解説→レポート
を回す。「ヒント」でヒント要求、「ギブアップ」で降参。

例:
    GOOGLE_API_KEY=... python -m qqquestion.cli --repo /path/to/repo
    QQQ_FAKE_LLM=1 python -m qqquestion.cli --demo   # APIキー不要のデモ
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import diff_analyzer
from .knowledge_base import (
    KnowledgeBaseBuilder,
    create_knowledge_base,
    create_search_provider,
)
from .learner_model import HistoryStore, load_learner_state
from .llm import create_llm
from .session import QuizSession

BANNER = """\
==========================================================
  QQQuestionAI 「答えは教えません。でも必ず説明できるようになります」
==========================================================
記述式で解答してください。困ったら「ヒント」、降参は「ギブアップ」。
"""


def run(repo_path: str, data_dir: str, diff_file: str | None, demo: bool) -> int:
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

    session = QuizSession(
        llm=create_llm(),
        kb=kb,
        diff_ctx=diff_ctx,
        learner_state=learner_state,
        history_store=HistoryStore(data / "history.jsonl"),
    )

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
                print("\n中断しました。コミットは中止されます。")
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
                result = session.give_up()
                print(f"\n正解は「{result.model_answer}」でした。")
                _print_explanation(result)
                break

            result = session.submit_answer(user_input)
            verdict = result.judgement.verdict
            if verdict == "correct":
                print(f"先生> 正解です！🎉 ({result.judgement.reason})")
                _print_explanation(result)
                break
            if verdict == "partial":
                print(f"先生> 部分的に正解です。{result.judgement.reason} もう一度どうぞ。")
            else:
                print("先生> 残念、違います。「ヒント」と言ってくれれば手がかりを出しますよ。")
        print()

    print(session.report().render())
    print("理解度チェック完走。コミットを続行します。")
    return 0


def _print_explanation(result) -> None:
    if result.explanation is None:
        return
    print("\n----- 解説 -----")
    print(result.explanation.explanation)
    if result.explanation.citations:
        print("出典:")
        for url in result.explanation.citations:
            print(f"  - {url}")
    print("----------------")


def main() -> None:
    from .envfile import load_env_file

    load_env_file()  # backend/.env から GOOGLE_API_KEY 等を読み込む（任意）
    parser = argparse.ArgumentParser(description="QQQuestionAI 理解度チェック CLI")
    parser.add_argument("--repo", default=".", help="対象リポジトリ")
    parser.add_argument("--data-dir", default="data", help="履歴・知識ベースの保存先")
    parser.add_argument("--diff-file", default=None, help="差分ファイルから出題（デバッグ用）")
    parser.add_argument("--demo", action="store_true", help="APIキー不要のデモモード")
    args = parser.parse_args()
    sys.exit(run(args.repo, args.data_dir, args.diff_file, args.demo))


if __name__ == "__main__":
    main()
