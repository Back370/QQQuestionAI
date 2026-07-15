"""ストリーミング(SSE)経路の独立QAハーネス。

Agent2 の qa_flow.py は非ストリーム API のみを検証していたため、
本ハーネスは未コミットの最大変更である半二重ストリーミング経路
(/quiz/{sid}/answer/stream, /giveup/stream) を対象に、
- SSE イベント順序と不正解時の途中経過抑止
- 値ベースの答え漏洩検出(キー名一致ではなく、accepted_points /
  model_answer の"中身"が問題完了前のレスポンスに現れないか)
- data/eval_set.json を用いた実判定経路のオフライン精度
- ストリーム版と非ストリーム版の最終結果の一致(パリティ)
を検証し、ログ・レポートを残す。すべて FakeLLM + インメモリで実行し、
外部ネットワーク・APIキーには触れない。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = PROJECT_ROOT / "backend"
ARTIFACT_DIR = PROJECT_ROOT / "qtest" / "qa_artifacts_stream"
LOG_PATH = ARTIFACT_DIR / "stream_flow_log.jsonl"
REPORT_PATH = ARTIFACT_DIR / "qa_stream_report.md"
EVAL_SET_PATH = BACKEND_DIR / "data" / "eval_set.json"

if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from fastapi.testclient import TestClient

from qqquestion.demo import DEMO_QUESTIONS, build_demo_llm
from qqquestion.diff_analyzer import analyze
from qqquestion.evaluator import evaluate_judge
from qqquestion.judge import judge_answer
from qqquestion.knowledge_base import InMemoryKnowledgeBase
from qqquestion.models import Chunk
from qqquestion.server import AppDeps, create_app
from qqquestion.session import QuizSession
from qqquestion.textutil import normalize

SAMPLE_DIFF = """\
diff --git a/rnn_train.py b/rnn_train.py
index 1111111..2222222 100644
--- a/rnn_train.py
+++ b/rnn_train.py
@@ -1,4 +1,20 @@
 import numpy as np
+for epoch in range(0, num_epoch):
+    for i in index:
+        Z_prime = np.zeros((q, T+1))
+        for t in range(T):
+            Z_prime[:, t+1], nabla_f[:, t] = forward(np.append(1, xi[t,:]), Z_prime[:, t], W_in, W, sigmoid)
+        z_out = softmax(np.dot(W_out, Z_T))
+        e[i] = CrossEntoropy(z_out, yi)
+        delta_out = z_out - yi
+        for t in reversed(range(T)):
+            delta[:, t] = backward(W, W_out[:, 1:], delta[:, t+1], np.zeros(m), nabla_f[:, t])
+        dEdW = np.dot(delta, Z_prime[:, :T].T)
"""

STREAM_SCENARIO = [
    {
        "id": "q1",
        "steps": [
            ("answer_stream", "隠れ層に再帰結合があり、前の時刻の隠れ状態を使って系列の文脈を保持できる点", "correct"),
        ],
    },
    {
        "id": "q2",
        "steps": [
            ("hint", None, None),
            ("answer_stream", "たぶん昼ごはんの話だと思います", "incorrect"),
            ("answer_stream", "予測確率分布と正解のone-hot分布の間の隔たりを測る", "correct"),
        ],
    },
    {
        "id": "q3",
        "steps": [
            ("answer_stream", "順伝播をしているだけです", "partial"),
            ("answer_stream", "各時刻の隠れ状態の保存と活性化関数の勾配の保存をしている", "correct"),
        ],
    },
    {
        "id": "q4",
        "steps": [
            ("answer_stream", "逆順のほうが見た目がきれいだからです", "incorrect"),
            ("hint", None, None),
            ("answer_stream", "delta[t+1]への依存があるため未来から過去の順で計算する", "correct"),
        ],
    },
    {
        "id": "q5",
        "steps": [
            ("answer_stream", "なんとなくです", "incorrect"),
            ("giveup_stream", None, "incorrect"),
        ],
    },
]


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_jsonl(entry: dict[str, Any]) -> None:
    with LOG_PATH.open("a", encoding="utf-8") as file:
        file.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")


def make_kb() -> InMemoryKnowledgeBase:
    kb = InMemoryKnowledgeBase()
    kb.add("RNN", [Chunk(text="RNN は再帰結合を持ち、前の時刻の隠れ状態を利用して系列を処理する。", url="https://example.com/rnn", title="RNN入門")])
    kb.add("クロスエントロピー", [Chunk(text="クロスエントロピーは予測確率分布と正解の one-hot 分布の隔たりを測る。", url="https://example.com/cross-entropy", title="クロスエントロピー")])
    kb.add("誤差逆伝播", [Chunk(text="BPTT では未来時刻の delta への依存をたどるため、時刻を逆順に進める。", url="https://example.com/bptt", title="BPTT")])
    kb.add("勾配計算", [Chunk(text="再帰重み W の勾配は各時刻の delta と 1 時刻前の隠れ状態を対応させて計算する。", url="https://example.com/gradient", title="RNN勾配")])
    return kb


def question_by_id(question_id: str):
    return next(question for question in DEMO_QUESTIONS if question.id == question_id)


def collect_strings(value: Any, path: str = "$") -> list[tuple[str, str]]:
    if isinstance(value, str):
        return [(path, value)]
    if isinstance(value, dict):
        found: list[tuple[str, str]] = []
        for key, item in value.items():
            found.extend(collect_strings(item, f"{path}.{key}"))
        return found
    if isinstance(value, list):
        found = []
        for i, item in enumerate(value):
            found.extend(collect_strings(item, f"{path}[{i}]"))
        return found
    return []


def find_value_leaks(payload: Any, forbidden: list[str], min_len: int = 2) -> list[dict[str, str]]:
    normalized_forbidden = [
        (item, normalize(item)) for item in forbidden if len(normalize(item)) >= min_len
    ]
    hits: list[dict[str, str]] = []
    for path, text in collect_strings(payload):
        normalized_text = normalize(text)
        for original, needle in normalized_forbidden:
            if needle in normalized_text:
                hits.append({"path": path, "leaked": original})
    return hits


def parse_sse(response) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for block in response.text.split("\n\n"):
        block = block.strip()
        if block.startswith("data: "):
            events.append(json.loads(block[len("data: "):]))
    return events


def scan_step_leaks(events: list[dict[str, Any]], question, done: bool) -> dict[str, Any]:
    if done:
        return {"model_answer_leak": [], "points_leak": [], "scanned": False}
    forbidden_answer = [question.model_answer]
    forbidden_points = list(question.accepted_points)
    model_leak: list[dict[str, str]] = []
    points_leak: list[dict[str, str]] = []
    for event in events:
        if event.get("model_answer"):
            model_leak.append({"path": "$.model_answer", "leaked": event["model_answer"]})
        scannable = {k: v for k, v in event.items() if k != "event"}
        model_leak.extend(find_value_leaks(scannable, forbidden_answer, min_len=4))
        points_leak.extend(find_value_leaks(scannable, forbidden_points, min_len=2))
    return {"model_answer_leak": model_leak, "points_leak": points_leak, "scanned": True}


def run_stream_flow(client: TestClient, session_id: str) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for scenario in STREAM_SCENARIO:
        question_body = client.get(f"/quiz/{session_id}/question").json()
        question_public = question_body["question"]
        model_question = question_by_id(question_public["id"])
        write_jsonl({"ts": now(), "event": "question", "response": question_body})
        step_records: list[dict[str, Any]] = []
        for action, payload, expected in scenario["steps"]:
            if action == "hint":
                response = client.post(f"/quiz/{session_id}/hint")
                body = response.json()
                leak = find_value_leaks(
                    body["hint"]["hint"],
                    [model_question.model_answer, *model_question.accepted_points],
                    min_len=4,
                )
                record = {
                    "action": action,
                    "status_code": response.status_code,
                    "hint_present": bool(body["hint"]["hint"]),
                    "answer_leak": bool(leak),
                }
                write_jsonl({"ts": now(), "event": action, "question_id": question_public["id"], "response": body, "checks": record})
            else:
                if action == "giveup_stream":
                    response = client.post(f"/quiz/{session_id}/giveup/stream")
                else:
                    response = client.post(
                        f"/quiz/{session_id}/answer/stream", json={"answer": payload}
                    )
                events = parse_sse(response)
                names = [event["event"] for event in events]
                verdict = next(
                    (e["judgement"]["verdict"] for e in events if e["event"] == "judgement"),
                    None,
                )
                done = any(e.get("question_done") for e in events)
                leak = scan_step_leaks(events, model_question, done)
                incorrect_stream_ok = True
                if expected == "incorrect" and action == "answer_stream":
                    incorrect_stream_ok = (
                        "judgement_partial" not in names
                        and "explanation_partial" not in names
                    )
                order_ok = True
                if "explanation_partial" in names:
                    order_ok = names.index("judgement") < names.index("explanation_partial")
                record = {
                    "action": action,
                    "status_code": response.status_code,
                    "content_type": response.headers.get("content-type", ""),
                    "answer": payload,
                    "expected_verdict": expected,
                    "actual_verdict": verdict,
                    "matched": verdict == expected,
                    "event_names": names,
                    "question_done": done,
                    "incorrect_stream_suppressed": incorrect_stream_ok,
                    "event_order_ok": order_ok,
                    "model_answer_leak": leak["model_answer_leak"],
                    "points_leak": leak["points_leak"],
                }
                write_jsonl({"ts": now(), "event": action, "question_id": question_public["id"], "sse_events": events, "checks": record})
            step_records.append(record)
        results.append(
            {
                "id": question_public["id"],
                "type": question_public["type"],
                "steps": step_records,
            }
        )
    return results


def evaluate_accuracy() -> dict[str, Any]:
    llm = build_demo_llm()
    result = evaluate_judge(lambda q, a: judge_answer(llm, q, a), EVAL_SET_PATH)
    write_jsonl({"ts": now(), "event": "accuracy", "response": result})
    return result


def check_parity() -> dict[str, Any]:
    diff_ctx = analyze(SAMPLE_DIFF)
    streamed = QuizSession(llm=build_demo_llm(), kb=make_kb(), diff_ctx=diff_ctx)
    oneshot = QuizSession(llm=build_demo_llm(), kb=make_kb(), diff_ctx=diff_ctx)
    question_id = streamed.current().question.id
    answer = "隠れ層に再帰結合があり、前の時刻の隠れ状態を使って系列の文脈を保持できる点"
    stream_result = [
        payload
        for name, payload in streamed.submit_answer_stream(answer)
        if name == "result"
    ][0]
    oneshot_result = oneshot.submit_answer(answer)
    parity = {
        "question_id": question_id,
        "judgement_equal": stream_result.judgement == oneshot_result.judgement,
        "model_answer_equal": stream_result.model_answer == oneshot_result.model_answer,
    }
    write_jsonl({"ts": now(), "event": "parity", "response": parity})
    return parity


def aggregate(flow_results: list[dict[str, Any]]) -> dict[str, Any]:
    verdict_steps = [
        step
        for result in flow_results
        for step in result["steps"]
        if "matched" in step
    ]
    model_leaks = [
        {"question": result["id"], **hit}
        for result in flow_results
        for step in result["steps"]
        for hit in step.get("model_answer_leak", [])
    ]
    points_leaks = [
        {"question": result["id"], **hit}
        for result in flow_results
        for step in result["steps"]
        for hit in step.get("points_leak", [])
    ]
    incorrect_streams = [
        step
        for result in flow_results
        for step in result["steps"]
        if step.get("expected_verdict") == "incorrect" and step["action"] == "answer_stream"
    ]
    sse_content_ok = all(
        step.get("content_type", "").startswith("text/event-stream")
        for result in flow_results
        for step in result["steps"]
        if step["action"] in ("answer_stream", "giveup_stream")
    )
    return {
        "scripted_verdicts_matched": sum(1 for s in verdict_steps if s["matched"]),
        "scripted_verdicts_total": len(verdict_steps),
        "model_answer_leaks": model_leaks,
        "points_leaks": points_leaks,
        "incorrect_reason_suppressed": all(
            s["incorrect_stream_suppressed"] for s in incorrect_streams
        ),
        "event_order_ok": all(
            step["event_order_ok"]
            for result in flow_results
            for step in result["steps"]
            if "event_order_ok" in step
        ),
        "sse_content_type_ok": sse_content_ok,
    }


def render_report(
    flow_results: list[dict[str, Any]],
    summary: dict[str, Any],
    accuracy: dict[str, Any],
    parity: dict[str, Any],
) -> str:
    overall = all(
        [
            summary["scripted_verdicts_matched"] == summary["scripted_verdicts_total"],
            not summary["model_answer_leaks"],
            not summary["points_leaks"],
            summary["incorrect_reason_suppressed"],
            summary["event_order_ok"],
            summary["sse_content_type_ok"],
            accuracy["accuracy"] == 1.0,
            parity["judgement_equal"],
            parity["model_answer_equal"],
        ]
    )
    lines = [
        "# QQQuestionAI ストリーミングQAレポート",
        "",
        f"総合判定: {'PASS' if overall else 'FAIL'}",
        f"実行時刻: {now()}",
        f"対象: {PROJECT_ROOT}",
        f"ログ: {LOG_PATH}",
        "",
        "## SSEシナリオ結果",
        "",
        "| 問題 | 操作 | 期待 | 実際 | SSEイベント | 判定 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for result in flow_results:
        for step in result["steps"]:
            if step["action"] == "hint":
                ok = "PASS" if step["hint_present"] and not step["answer_leak"] else "FAIL"
                lines.append(f"| {result['id']} | hint | - | - | - | {ok} |")
                continue
            expected = step.get("expected_verdict", "-")
            actual = step.get("actual_verdict", "-")
            names = "/".join(step.get("event_names", []))
            ok = "PASS"
            if not step.get("matched", True):
                ok = "FAIL"
            if step.get("model_answer_leak") or step.get("points_leak"):
                ok = "FAIL"
            if not step.get("incorrect_stream_suppressed", True):
                ok = "FAIL"
            lines.append(f"| {result['id']} | {step['action']} | {expected} | {actual} | {names} | {ok} |")
    lines.extend(
        [
            "",
            "## 妥当性(ストリーミング固有)",
            "",
            f"- スクリプト期待判定一致: {summary['scripted_verdicts_matched']}/{summary['scripted_verdicts_total']}",
            f"- SSE Content-Type text/event-stream: {'PASS' if summary['sse_content_type_ok'] else 'FAIL'}",
            f"- 判定→解説のイベント順序: {'PASS' if summary['event_order_ok'] else 'FAIL'}",
            f"- 不正解時に理由/解説の途中経過を流さない: {'PASS' if summary['incorrect_reason_suppressed'] else 'FAIL'}",
            "",
            "## 正確性",
            "",
            f"- 実eval_set.json 判定精度: {accuracy['correct']}/{accuracy['total']} ({accuracy['accuracy']:.0%})",
            f"- 判定失敗: {json.dumps(accuracy['failures'], ensure_ascii=False)}",
            f"- ストリーム/非ストリーム判定一致: {'PASS' if parity['judgement_equal'] else 'FAIL'}",
            f"- ストリーム/非ストリーム模範解答一致: {'PASS' if parity['model_answer_equal'] else 'FAIL'}",
            "",
            "## セキュリティ(値ベース漏洩検出)",
            "",
            f"- 問題完了前の模範解答(本文)の漏洩: {len(summary['model_answer_leaks'])} 件",
            f"- 問題完了前の accepted_points(要点)の漏洩: {len(summary['points_leaks'])} 件",
            f"- 漏洩詳細(要点): {json.dumps(summary['points_leaks'], ensure_ascii=False)}",
            "",
            "## 不確実性・制約",
            "",
            "- FakeLLM 決定的判定であり、Gemini実APIのストリーム挙動・品質は対象外。",
            "- 値ベース検出は正規化包含近似のため、極端に短い要点は誤検知回避のため min_len で除外。",
            "- VSCode Webview 実表示・実SSEネットワークは対象外(HTTPレイヤの契約のみ検証)。",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    LOG_PATH.write_text("", encoding="utf-8")
    diff_ctx = analyze(SAMPLE_DIFF)
    deps = AppDeps(
        llm=build_demo_llm(),
        kb=make_kb(),
        data_dir=ARTIFACT_DIR,
        diff_provider=lambda repo: diff_ctx,
    )
    client = TestClient(create_app(deps))
    start = client.post("/quiz/start", json={"repo_path": str(PROJECT_ROOT)})
    write_jsonl({"ts": now(), "event": "start", "response": start.json()})
    session_id = start.json()["session_id"]

    flow_results = run_stream_flow(client, session_id)
    summary = aggregate(flow_results)
    accuracy = evaluate_accuracy()
    parity = check_parity()
    report = render_report(flow_results, summary, accuracy, parity)
    REPORT_PATH.write_text(report, encoding="utf-8")
    print(report)
    return 1 if "総合判定: FAIL" in report else 0


if __name__ == "__main__":
    raise SystemExit(main())
