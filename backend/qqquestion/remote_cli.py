"""起動済みバックエンドに HTTP でつないでターミナルからクイズを行うクライアント。

VSCode 拡張の利用者向け。拡張は API キーを VSCode の SecretStorage に保存し、
バックエンドプロセスへ環境変数として渡している。ターミナル側からその秘密は
読めないため、ローカルで LLM を組み立てる `cli.py` は拡張利用者には使えない。
そこで**キーを持っているバックエンドに実行を委譲する**のがこのモジュール。
ターミナル側に API キーを置かずに済む（AGENTS.md 安全ルール2）。

コミットは一切行わない。git とは無関係にいつでも実行できる。

    python -m qqquestion.remote_cli                    # ステージ済み差分から出題
    python -m qqquestion.remote_cli --list-models      # 使えるモデルを一覧表示
    python -m qqquestion.remote_cli --model gemini-...  # モデルを切り替えて出題
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Iterator

from .learner_model import format_learner_summary
from .terminput import enable_line_editing

DEFAULT_PORT = 8756
# 通常のエンドポイント（メモリ参照のみ）
TIMEOUT = 10.0
# LLM 生成を伴うエンドポイント
LLM_TIMEOUT = 120.0

BANNER = """\
==========================================================
  QQQuestionAI 「答えは教えません。でも必ず説明できるようになります」
==========================================================
記述式で解答してください。困ったら「ヒント」、降参は「ギブアップ」。
（このクイズはコミットを行いません）
"""


class BackendUnavailable(RuntimeError):
    """バックエンドに接続できない。"""


def _base_url(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def _health(client, port: int) -> bool:
    try:
        response = client.get(f"{_base_url(port)}/health", timeout=2.0)
        return response.status_code == 200
    except Exception:
        return False


class _TypeWriter:
    """スナップショット（毎回全文）を受け取り、増分だけを逐次表示する。"""

    def __init__(self) -> None:
        self._shown = ""

    @property
    def started(self) -> bool:
        return bool(self._shown)

    def update(self, text: str) -> None:
        if text.startswith(self._shown):
            delta = text[len(self._shown) :]
        else:
            delta = "\n" + text
        if delta:
            print(delta, end="", flush=True)
            self._shown = text


def _iter_sse(response) -> Iterator[dict]:
    """`data: {json}\\n\\n` 形式の SSE を辞書として取り出す。"""
    buffer = ""
    for chunk in response.iter_text():
        buffer += chunk
        while "\n\n" in buffer:
            raw, buffer = buffer.split("\n\n", 1)
            raw = raw.strip()
            if raw.startswith("data: "):
                yield json.loads(raw[len("data: ") :])


def _consume_stream(client, url: str, json_body: dict | None = None) -> dict | None:
    """SSE を逐次表示しながら最終結果 (result イベント) を返す。"""
    reason = _TypeWriter()
    explanation = _TypeWriter()
    result: dict | None = None
    with client.stream("POST", url, json=json_body, timeout=LLM_TIMEOUT) as response:
        response.raise_for_status()
        for event in _iter_sse(response):
            name = event.get("event")
            if name == "judgement_partial":
                if not reason.started:
                    print("先生> ", end="", flush=True)
                reason.update(event.get("reason", ""))
            elif name == "judgement":
                if reason.started:
                    print()
                _print_verdict(event, streamed_reason=reason.started)
            elif name == "explanation_partial":
                if not explanation.started:
                    print("\n----- 解説 -----")
                explanation.update(event.get("explanation", ""))
            elif name == "result":
                result = event
    if explanation.started:
        print()
        citations = ((result or {}).get("explanation") or {}).get("citations") or []
        if citations:
            print("出典:")
            for url_ in citations:
                print(f"  - {url_}")
        print("----------------")
    return result


def _print_verdict(payload: dict, streamed_reason: bool) -> None:
    judgement = payload.get("judgement") or {}
    verdict = judgement.get("verdict")
    reason = judgement.get("reason", "")
    if verdict == "correct":
        suffix = "" if streamed_reason else f" ({reason})"
        print(f"先生> 正解です！🎉{suffix}")
    elif verdict == "partial":
        middle = "" if streamed_reason else f"{reason} "
        print(
            f"先生> 部分的に正解です。{middle}"
            "正解済みの部分は繰り返さなくてよいので、足りない部分だけ補足してください。"
        )
    elif payload.get("question_done"):  # ギブアップ
        print(f"\n正解は「{payload.get('model_answer')}」でした。")
    else:
        print("先生> 残念、違います。「ヒント」と言ってくれれば手がかりを出しますよ。")


def print_models(client, port: int) -> None:
    """バックエンドが把握しているモデル一覧を表示する（* が現在のモデル）。

    一覧の取得には API キーが要るが、キーはバックエンド側にしか無いので
    ここでも HTTP 越しに訊く（ターミナルに秘密を置かない方針）。
    """
    body = client.get(f"{_base_url(port)}/models", timeout=LLM_TIMEOUT).json()
    if body.get("source") == "fallback":
        print("（APIから一覧を取得できませんでした。内蔵の候補を表示します）")
    for entry in body.get("models", []):
        mark = "*" if entry["name"] == body.get("current") else " "
        note = entry.get("description") or entry.get("label") or ""
        print(f" {mark} {entry['name']}  {note}".rstrip())
    print("\n切り替え: quiz --model <モデル名>（VSCode の設定 qqquestion.model でも変更可）")


def select_model(client, port: int, model: str) -> str:
    """バックエンドの使用モデルを切り替える（再起動不要）。

    切り替えはバックエンドのプロセス全体に効くため、VSCode 側のクイズにも
    同じモデルが使われる。次回起動時に戻したくない場合は設定 qqquestion.model を使う。
    """
    body = client.post(
        f"{_base_url(port)}/models/select", json={"model": model}, timeout=TIMEOUT
    )
    body.raise_for_status()
    return body.json()["current"]


def _print_question(view: dict) -> None:
    type_label = "前提知識" if view["type"] == "prerequisite" else "実装の説明"
    print(f"【第{view['number']}問/{view['total']}】({type_label}・難易度{view['difficulty']})")
    print(view["text"])
    if view.get("code_snippet"):
        print()
        for line in view["code_snippet"].splitlines():
            print(f"    {line}")
    print()


def _wait_for_question(client, port: int, sid: str) -> dict | None:
    """生成中なら出題できるまで待つ。返り値 None は全問終了。"""
    notified = False
    while True:
        body = client.get(f"{_base_url(port)}/quiz/{sid}/question", timeout=TIMEOUT).json()
        if body.get("question"):
            return body["question"]
        if body.get("status") in ("completed", "aborted"):
            return None
        if body.get("error"):
            print(f"\n出題できませんでした: {body['error']}")
            return None
        if not notified:
            print("問題を生成中です...", flush=True)
            notified = True
        time.sleep(1.0)


def run(repo: str, port: int, model: str | None = None, list_models: bool = False) -> int:
    enable_line_editing()  # input() を日本語（マルチバイト）でも1文字ずつ削除できるようにする
    try:
        import httpx
    except ModuleNotFoundError:
        print("httpx が見つかりません。バックエンドの依存が壊れています。", file=sys.stderr)
        return 1

    with httpx.Client() as client:
        if not _health(client, port):
            print(
                "QQQuestionAI: バックエンドに接続できません "
                f"(127.0.0.1:{port})。\n"
                "  VSCode で QQQuestionAI 拡張が動いているウィンドウを開いてから、"
                "もう一度お試しください。\n"
                "  （拡張がバックエンドを自動起動します。API キーは拡張側に保存されています）",
                file=sys.stderr,
            )
            return 1

        if list_models:
            print_models(client, port)
            return 0
        if model:
            try:
                print(f"使用モデルを {select_model(client, port, model)} に切り替えました。")
            except Exception as error:
                print(f"モデルを切り替えられませんでした: {error}", file=sys.stderr)
                return 1

        response = client.post(
            f"{_base_url(port)}/quiz/start",
            # origin="cli": 出題はこの端末で行う。拡張にパネルを開かせない
            json={"repo_path": os.path.abspath(repo), "origin": "cli"},
            timeout=LLM_TIMEOUT,
        )
        if response.status_code == 400:
            print("ステージ済みの差分がありません。git add してから実行してください。", file=sys.stderr)
            return 1
        response.raise_for_status()
        body = response.json()
        sid = body["session_id"]

        print(BANNER)
        if body.get("model"):
            print(f"使用モデル: {body['model']}（--model で切り替え / --list-models で一覧）")
        print(f"対象差分: {', '.join(body.get('files') or []) or '(不明)'}")
        print(f"抽出トピック: {' / '.join(body.get('topics') or []) or '(なし)'}")
        for line in format_learner_summary(
            body.get("weak_topics") or [],
            body.get("priority_topics") or [],
            body.get("overcome_topics") or [],
            body.get("weak_topic_scores") or {},
        ):
            print(line)
        if body.get("error"):
            print(f"\n警告: {body['error']}")
        print()

        while True:
            view = _wait_for_question(client, port, sid)
            if view is None:
                break
            _print_question(view)
            while True:
                try:
                    user_input = input("あなた> ").strip()
                except (EOFError, KeyboardInterrupt):
                    print("\n中断しました。（コミットには影響しません）")
                    client.post(f"{_base_url(port)}/quiz/{sid}/abort", timeout=TIMEOUT)
                    return 1
                if not user_input:
                    continue
                if user_input in ("ヒント", "hint"):
                    hint_body = client.post(
                        f"{_base_url(port)}/quiz/{sid}/hint", timeout=LLM_TIMEOUT
                    ).json()["hint"]
                    print(f"先生(ヒント)> {hint_body['hint']}")
                    for url_ in hint_body.get("citations") or []:
                        print(f"  出典: {url_}")
                    continue
                if user_input in ("ギブアップ", "giveup"):
                    _consume_stream(client, f"{_base_url(port)}/quiz/{sid}/giveup/stream")
                    break
                result = (
                    _consume_stream(
                        client,
                        f"{_base_url(port)}/quiz/{sid}/answer/stream",
                        {"answer": user_input},
                    )
                    or {}
                )
                if (result.get("judgement") or {}).get("verdict") == "correct":
                    break
            print()

        report = client.get(f"{_base_url(port)}/quiz/{sid}/report", timeout=LLM_TIMEOUT).json()
        print(report.get("rendered", ""))
        return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="QQQuestionAI 理解度チェック（起動済みバックエンドに接続。コミットはしません）"
    )
    parser.add_argument("--repo", default=".", help="対象リポジトリ")
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("QQQ_PORT", DEFAULT_PORT)),
        help="バックエンドのポート",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="使用するモデルを切り替えてから出題する（バックエンド全体に効く）",
    )
    parser.add_argument(
        "--list-models", action="store_true", help="使えるモデルを一覧表示して終了"
    )
    args = parser.parse_args()
    sys.exit(run(args.repo, args.port, args.model, args.list_models))


if __name__ == "__main__":
    main()
