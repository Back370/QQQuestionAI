"""起動済みバックエンドに HTTP でつないでターミナルからクイズを行うクライアント。

VSCode 拡張の利用者向け。拡張は API キーを VSCode の SecretStorage に保存し、
バックエンドプロセスへ環境変数として渡している。ターミナル側からその秘密は
読めないため、ローカルで LLM を組み立てる `cli.py` は拡張利用者には使えない。
そこで**キーを持っているバックエンドに実行を委譲する**のがこのモジュール。
ターミナル側に API キーを置かずに済む（AGENTS.md 安全ルール2）。

コミットは一切行わない。git とは無関係にいつでも実行できる。

    python -m qqquestion.remote_cli            # ステージ済み差分から出題
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Iterator

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


def run(repo: str, port: int) -> int:
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
        print(f"対象差分: {', '.join(body.get('files') or []) or '(不明)'}")
        print(f"抽出トピック: {' / '.join(body.get('topics') or []) or '(なし)'}")
        if body.get("weak_topics"):
            print(f"前回の苦手傾向: {' / '.join(body['weak_topics'])} → 優先出題します")
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
    args = parser.parse_args()
    sys.exit(run(args.repo, args.port))


if __name__ == "__main__":
    main()
