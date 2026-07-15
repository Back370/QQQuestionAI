"""quiz コマンド相当を「実際にコミットせず」HTTP で自動実行する E2E ハーネス。

想定フロー（テスト自動化）:
  1. qtest/test.go を編集する（作業ツリーは汚さず、使い捨ての一時 git リポジトリに
     複製してから追記・git add する。実コミットはしない＝安全）。
  2. quiz を起点だけ発火（POST /quiz/start）。フック(git commit -q)と同じ HTTP 経路。
  3. 第1問に自動で答える（ヒント要求→解答提出）。
  4. 入力（解答）・出力（判定/模範解答/解説）・ヒントをログファイルに記録する。
  5. クイズを終了する（POST /quiz/{sid}/abort）。

バックエンドは「実サーバ」を HTTP で叩く。起動中のサーバがあれば QQQ_E2E_BASE で
繋ぎ、無ければ本ハーネスが FakeLLM で uvicorn を一時起動し、終了時に片付ける。
FakeLLM を使うので API キー・外部ネットワークは不要で決定的に回る。

実行:
    cd backend && .venv/bin/python ../qtest/quiz_e2e.py
    # 既存サーバに繋ぐ場合:
    QQQ_E2E_BASE=http://127.0.0.1:8756 .venv/bin/python ../qtest/quiz_e2e.py
環境変数:
    QQQ_E2E_BASE  接続先。指定すると自動起動せずそのサーバを使う。
    QQQ_E2E_PORT  自動起動時のポート（既定 8799。開発用 8756 と衝突させない）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = PROJECT_ROOT / "backend"
SOURCE_GO = PROJECT_ROOT / "qtest" / "test.go"
ARTIFACT_DIR = PROJECT_ROOT / "qtest" / "qa_artifacts_e2e"
LOG_PATH = ARTIFACT_DIR / "quiz_e2e_log.jsonl"
REPORT_PATH = ARTIFACT_DIR / "quiz_e2e_report.md"

# 第1問（FakeLLM の缶詰 RNN 前提知識問題）に対する自動解答。
# わざと不十分な解答→ヒント要求→十分な解答、の順で 1 問を回し切る。
PARTIAL_ANSWER = "再帰結合を持つところが違います。"
FULL_ANSWER = (
    "隠れ層が再帰結合を持ち、前の時刻の隠れ状態を次の時刻の入力に使うことで"
    "系列の文脈を保持できる点が違います。"
)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_jsonl(entry: dict[str, Any]) -> None:
    with LOG_PATH.open("a", encoding="utf-8") as file:
        file.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")


# --- HTTP（標準ライブラリのみ。追加依存なし） --------------------------------

def http(base: str, method: str, path: str, body: dict | None = None,
         timeout: float = 130.0) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        base + path, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode() or "{}")


def wait_health(base: str, deadline_s: float = 20.0) -> bool:
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        try:
            code, _ = http(base, "GET", "/health", timeout=2.0)
            if code == 200:
                return True
        except (urllib.error.URLError, ConnectionError, OSError):
            pass
        time.sleep(0.3)
    return False


# --- 一時 git リポジトリ（test.go を編集する。作業ツリーは触らない） ----------

def make_temp_repo(tmp: Path) -> Path:
    repo = tmp / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    original = SOURCE_GO.read_text(encoding="utf-8") if SOURCE_GO.exists() else "package main\n"
    edited = original + (
        "\n// --- quiz_e2e による編集（自動テスト用の追記） ---\n"
        "func requireBotForTest() bool {\n"
        "\treturn true\n"
        "}\n"
    )
    (repo / "test.go").write_text(edited, encoding="utf-8")
    subprocess.run(["git", "add", "test.go"], cwd=repo, check=True)
    return repo


# --- サーバ（実サーバを HTTP で。未起動なら FakeLLM で一時起動） --------------

def start_server(port: int, data_dir: Path) -> subprocess.Popen:
    env = dict(os.environ)
    env.update(
        QQQ_FAKE_LLM="1",       # API キー不要・決定的
        QQQ_NO_SEARCH="1",      # 外部ネットワークに触れない
        QQQ_PORT=str(port),
        QQQ_DATA_DIR=str(data_dir),
    )
    return subprocess.Popen(
        [sys.executable, "-m", "qqquestion.server"],
        cwd=str(BACKEND_DIR), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


# --- クイズ 1 問を回す ---------------------------------------------------------

def fetch_question(base: str, sid: str, deadline_s: float = 30.0) -> dict:
    """第1問が生成されるまで /question をポーリングして返す。"""
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        _, body = http(base, "GET", f"/quiz/{sid}/question")
        if body.get("question"):
            return body
        if body.get("error"):
            raise RuntimeError(f"出題に失敗: {body['error']}")
        time.sleep(0.3)
    raise TimeoutError("第1問が生成されませんでした")


def run_one_question(base: str, sid: str) -> dict[str, Any]:
    question_body = fetch_question(base, sid)
    question = question_body["question"]
    write_jsonl({"ts": now(), "event": "question", "response": question_body})

    steps: list[dict[str, Any]] = []

    # 1) わざと不十分な解答（入力）→ 判定（出力）
    _, partial = http(base, "POST", f"/quiz/{sid}/answer", {"answer": PARTIAL_ANSWER})
    steps.append({"kind": "answer", "input": PARTIAL_ANSWER, "output": partial})
    write_jsonl({"ts": now(), "event": "answer", "question_id": question["id"],
                 "input": PARTIAL_ANSWER, "output": partial})

    # 2) ヒント要求（ヒントを記録）
    _, hint = http(base, "POST", f"/quiz/{sid}/hint")
    steps.append({"kind": "hint", "output": hint})
    write_jsonl({"ts": now(), "event": "hint", "question_id": question["id"],
                 "output": hint})

    # 3) 十分な解答（入力）→ 判定・模範解答・解説（出力）
    _, full = http(base, "POST", f"/quiz/{sid}/answer", {"answer": FULL_ANSWER})
    steps.append({"kind": "answer", "input": FULL_ANSWER, "output": full})
    write_jsonl({"ts": now(), "event": "answer", "question_id": question["id"],
                 "input": FULL_ANSWER, "output": full})

    return {"question": question, "steps": steps}


# --- レポート ------------------------------------------------------------------

def render_report(result: dict[str, Any], files: list[str], topics: list[str]) -> str:
    question = result["question"]
    lines = [
        "# QQQuestionAI quiz 自動E2Eレポート",
        "",
        f"実行時刻: {now()}",
        f"対象差分ファイル: {', '.join(files) or '(不明)'}",
        f"抽出トピック: {' / '.join(topics) or '(なし)'}",
        f"ログ: {LOG_PATH}",
        "",
        f"## 第{question.get('number', 1)}問（{question['type']}）",
        "",
        f"> {question['text']}",
        "",
        "| # | 種別 | 入力 | 出力(要約) |",
        "| - | ---- | ---- | ---------- |",
    ]
    for i, step in enumerate(result["steps"], 1):
        if step["kind"] == "hint":
            hint = step["output"].get("hint", {})
            summary = hint.get("hint", "")
            lines.append(f"| {i} | ヒント | - | {summary} |")
        else:
            out = step["output"]
            verdict = out.get("judgement", {}).get("verdict", "?")
            done = out.get("question_done")
            extra = " / 模範解答・解説あり" if done else ""
            lines.append(f"| {i} | 解答 | {step['input']} | 判定={verdict}{extra} |")

    final = result["steps"][-1]["output"]
    lines += [
        "",
        "## 最終出力（問題完了時に開示された内容）",
        "",
        f"- 模範解答: {final.get('model_answer') or '(未開示)'}",
        f"- 解説: {(final.get('explanation') or {}).get('explanation', '(なし)')}",
        "",
        "## 制約・注意",
        "",
        "- 実コミットはしない。一時 git リポジトリに test.go を複製・編集して発火する。",
        "- 既定は FakeLLM（決定的・API キー/外部ネットワーク不要）。実 LLM の品質は対象外。",
        "- 1 問だけ回して abort で終了する（残りの問題は生成待ちのまま破棄）。",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    LOG_PATH.write_text("", encoding="utf-8")

    base = os.environ.get("QQQ_E2E_BASE", "").rstrip("/")
    server: subprocess.Popen | None = None
    started_here = False

    with tempfile.TemporaryDirectory(prefix="qqq-e2e-") as tmp_name:
        tmp = Path(tmp_name)
        repo = make_temp_repo(tmp)
        write_jsonl({"ts": now(), "event": "edit_test_go", "repo": str(repo)})

        if not base:
            port = int(os.environ.get("QQQ_E2E_PORT", "8799"))
            base = f"http://127.0.0.1:{port}"
            server = start_server(port, tmp / "server_data")
            started_here = True

        try:
            if not wait_health(base):
                print(f"サーバに接続できませんでした: {base}", file=sys.stderr)
                return 1

            code, start = http(base, "POST", "/quiz/start", {"repo_path": str(repo)})
            write_jsonl({"ts": now(), "event": "start", "status_code": code,
                         "response": start})
            if code != 200 or not start.get("session_id"):
                print(f"quiz を開始できませんでした（{code}）: {start}", file=sys.stderr)
                return 1
            sid = start["session_id"]

            result = run_one_question(base, sid)

            # クイズを終了
            _, aborted = http(base, "POST", f"/quiz/{sid}/abort")
            write_jsonl({"ts": now(), "event": "abort", "response": aborted})

            report = render_report(result, start.get("files", []), start.get("topics", []))
            REPORT_PATH.write_text(report, encoding="utf-8")
            print(report)
            print(f"ログ: {LOG_PATH}")
            print(f"レポート: {REPORT_PATH}")
            return 0
        finally:
            if server is not None and started_here:
                server.terminate()
                try:
                    server.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    server.kill()


if __name__ == "__main__":
    raise SystemExit(main())
