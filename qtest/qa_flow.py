from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = PROJECT_ROOT / "backend"
QTEST_DIR = PROJECT_ROOT / "qtest"
ARTIFACT_DIR = QTEST_DIR / "qa_artifacts"
LOG_PATH = ARTIFACT_DIR / "flow_log.jsonl"
REPORT_PATH = ARTIFACT_DIR / "qa_report.md"

if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from fastapi.testclient import TestClient

from qqquestion.demo import DEMO_QUESTIONS, build_demo_llm
from qqquestion.diff_analyzer import analyze
from qqquestion.evaluator import build_report
from qqquestion.judge import judge_answer
from qqquestion.knowledge_base import InMemoryKnowledgeBase
from qqquestion.models import Chunk, Interaction
from qqquestion.server import AppDeps, create_app
from qqquestion.textutil import contains_answer

SAMPLE_DIFF = """\
diff --git a/rnn_train.py b/rnn_train.py
index 1111111..2222222 100644
--- a/rnn_train.py
+++ b/rnn_train.py
@@ -1,4 +1,30 @@
 import numpy as np
+print("学習を行っています...")
+for epoch in range(0, num_epoch):
+    index = np.random.permutation(n_train)
+    e = np.full(n_train, np.nan)
+    for i in index:
+        xi = x_train[i]
+        yi = y_train_vec[i]
+        T = xi.shape[0]
+        Z_prime = np.zeros((q, T+1))
+        nabla_f = np.zeros((q, T))
+        for t in range(T):
+            Z_prime[:, t+1], nabla_f[:, t] = forward(np.append(1, xi[t,:]), Z_prime[:, t], W_in, W, sigmoid)
+        Z_T = np.append(1, Z_prime[:, T])
+        z_out = softmax(np.dot(W_out, Z_T))
+        e[i] = CrossEntoropy(z_out, yi)
+        if epoch == 0:
+            continue
+        delta_out = z_out - yi
+        delta = np.zeros((q, T))
+        for t in reversed(range(T)):
+            if t == T-1:
+                delta[:, t] = backward(W, W_out[:, 1:], np.zeros(q), delta_out, nabla_f[:, t])
+            else:
+                delta[:, t] = backward(W, W_out[:, 1:], delta[:, t+1], np.zeros(m), nabla_f[:, t])
+        dEdW_out = np.outer(delta_out, Z_T)
+        X = np.hstack((np.ones(T).reshape(-1, 1), xi))
+        dEdW_in = np.dot(delta, X)
+        dEdW = np.dot(delta, Z_prime[:, :T].T)
"""

SCENARIO = [
    {"id": "q1", "steps": [("answer", "隠れ層に再帰結合があり、前の時刻の隠れ状態を使って系列の文脈を保持できる点", "correct")]},
    {"id": "q2", "steps": [("hint", None, None), ("answer", "予測確率分布と正解のone-hot分布の間の隔たりを測る", "correct")]},
    {"id": "q3", "steps": [("answer", "今日は天気が良いので速くなります", "incorrect"), ("hint", None, None), ("answer", "順伝播を回して隠れ状態と活性化関数の勾配を保存している", "correct")]},
    {"id": "q4", "steps": [("answer", "逆順にした方がなんとなく自然だからです", "incorrect"), ("hint", None, None), ("answer", "delta[t+1]への依存があるため未来から過去の順で計算する", "correct")]},
    {"id": "q5", "steps": [("hint", None, None), ("answer", "行列の形を合わせるだけです", "incorrect"), ("giveup", None, "incorrect")]},
]

ACCURACY_CASES = [
    ("q1", "隠れ層に再帰結合があり、前の時刻の隠れ状態を使って系列の文脈を保持できる点", "correct"),
    ("q1", "再帰結合があるから", "partial"),
    ("q1", "層が深いところが違います", "incorrect"),
    ("q4", "delta[t+1]への依存があり、未来から過去へ計算する必要があるから", "correct"),
    ("q4", "そういう決まりだから", "incorrect"),
]

SECRET_PATTERN = re.compile(r"(?:[A-Za-z0-9_]*API[_-]?KEY|BEGIN [A-Z ]*PRIVATE KEY|sk-[A-Za-z0-9_-]{20,})")
FORBIDDEN_PUBLIC_KEYS = {"model_answer", "accepted_points", "rubric"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_jsonl(path: Path, entry: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as file:
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


def has_forbidden_key(value: Any) -> bool:
    if isinstance(value, dict):
        return bool(FORBIDDEN_PUBLIC_KEYS & set(value)) or any(has_forbidden_key(item) for item in value.values())
    if isinstance(value, list):
        return any(has_forbidden_key(item) for item in value)
    return False


def start_client() -> tuple[TestClient, str]:
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    LOG_PATH.write_text("", encoding="utf-8")
    history_path = ARTIFACT_DIR / "history.jsonl"
    history_path.write_text("", encoding="utf-8")
    diff_ctx = analyze(SAMPLE_DIFF)
    deps = AppDeps(llm=build_demo_llm(), kb=make_kb(), data_dir=ARTIFACT_DIR, diff_provider=lambda repo: diff_ctx)
    client = TestClient(create_app(deps))
    response = client.post("/quiz/start", json={"repo_path": str(PROJECT_ROOT)})
    write_jsonl(LOG_PATH, {"ts": now(), "event": "start", "status_code": response.status_code, "response": response.json()})
    response.raise_for_status()
    return client, response.json()["session_id"]


def run_flow(client: TestClient, session_id: str) -> list[dict[str, Any]]:
    results = []
    pending = client.get("/quiz/pending")
    write_jsonl(LOG_PATH, {"ts": now(), "event": "pending", "status_code": pending.status_code, "response": pending.json()})
    for scenario in SCENARIO:
        question_response = client.get(f"/quiz/{session_id}/question")
        question_body = question_response.json()
        question = question_body["question"]
        question_result = {
            "id": question["id"],
            "number": question["number"],
            "type": question["type"],
            "topic": question["topic"],
            "expected_id": scenario["id"],
            "question_public_leak": has_forbidden_key(question),
            "steps": [],
        }
        write_jsonl(LOG_PATH, {"ts": now(), "event": "question", "status_code": question_response.status_code, "response": question_body})
        for action, payload, expected in scenario["steps"]:
            if action == "hint":
                response = client.post(f"/quiz/{session_id}/hint")
                body = response.json()
                model_question = question_by_id(question["id"])
                leak = contains_answer(body["hint"]["hint"], [model_question.model_answer, *model_question.accepted_points])
                step = {"action": action, "status_code": response.status_code, "hint_present": bool(body["hint"]["hint"]), "citations": body["hint"].get("citations", []), "answer_leak": leak}
            elif action == "giveup":
                response = client.post(f"/quiz/{session_id}/giveup")
                body = response.json()
                verdict = body["judgement"]["verdict"]
                step = {"action": action, "status_code": response.status_code, "expected_verdict": expected, "actual_verdict": verdict, "matched": verdict == expected, "question_done": body["question_done"], "model_answer_revealed": bool(body.get("model_answer"))}
            else:
                response = client.post(f"/quiz/{session_id}/answer", json={"answer": payload})
                body = response.json()
                verdict = body["judgement"]["verdict"]
                step = {"action": action, "status_code": response.status_code, "answer": payload, "expected_verdict": expected, "actual_verdict": verdict, "matched": verdict == expected, "question_done": body["question_done"], "premature_answer_leak": (not body["question_done"] and has_forbidden_key(body))}
            question_result["steps"].append(step)
            write_jsonl(LOG_PATH, {"ts": now(), "event": action, "question_id": question["id"], "request": payload, "status_code": response.status_code, "response": body, "checks": step})
        results.append(question_result)
    status = client.get(f"/quiz/{session_id}/status")
    report = client.get(f"/quiz/{session_id}/report")
    write_jsonl(LOG_PATH, {"ts": now(), "event": "status", "status_code": status.status_code, "response": status.json()})
    write_jsonl(LOG_PATH, {"ts": now(), "event": "report", "status_code": report.status_code, "response": report.json()})
    return results


def evaluate_accuracy() -> dict[str, Any]:
    llm = build_demo_llm()
    failures = []
    for question_id, answer, expected in ACCURACY_CASES:
        actual = judge_answer(llm, question_by_id(question_id), answer).verdict
        if actual != expected:
            failures.append({"question_id": question_id, "answer": answer, "expected": expected, "actual": actual})
    total = len(ACCURACY_CASES)
    return {"total": total, "passed": total - len(failures), "accuracy": (total - len(failures)) / total if total else 0.0, "failures": failures}


def load_history_interactions() -> list[Interaction]:
    history_path = ARTIFACT_DIR / "history.jsonl"
    interactions = []
    for line in history_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            interactions.append(Interaction.model_validate_json(line))
    return interactions


def build_security_results(flow_results: list[dict[str, Any]]) -> dict[str, Any]:
    serialized_log = LOG_PATH.read_text(encoding="utf-8")
    hint_leaks = [step for result in flow_results for step in result["steps"] if step["action"] == "hint" and step["answer_leak"]]
    premature_leaks = [step for result in flow_results for step in result["steps"] if step.get("premature_answer_leak")]
    public_leaks = [result["id"] for result in flow_results if result["question_public_leak"]]
    return {
        "public_question_answer_leaks": public_leaks,
        "premature_response_answer_leaks": premature_leaks,
        "hint_answer_leaks": hint_leaks,
        "secret_like_patterns_in_log": len(SECRET_PATTERN.findall(serialized_log)),
        "network_mode": "in-process FastAPI TestClient + InMemoryKnowledgeBase",
    }


def build_validity_results(flow_results: list[dict[str, Any]], session_report: dict[str, Any]) -> dict[str, Any]:
    ids_match = [result["id"] == result["expected_id"] for result in flow_results]
    types = [result["type"] for result in flow_results]
    verdict_steps = [step for result in flow_results for step in result["steps"] if "matched" in step]
    return {
        "five_questions_completed": session_report["attempted"] == 5 and session_report["completed"],
        "scenario_order_matches": all(ids_match),
        "question_type_structure": types,
        "type_structure_valid": types[:2] == ["prerequisite", "prerequisite"] and types[2:] == ["implementation", "implementation", "implementation"],
        "scripted_verdicts_matched": sum(1 for step in verdict_steps if step["matched"]),
        "scripted_verdicts_total": len(verdict_steps),
    }


def render_report(flow_results: list[dict[str, Any]], validity: dict[str, Any], accuracy: dict[str, Any], security: dict[str, Any], session_report: dict[str, Any]) -> str:
    lines = [
        "# QQQuestionAI QA Flow Report",
        "",
        f"実行時刻: {now()}",
        f"対象: {PROJECT_ROOT}",
        f"ログ: {LOG_PATH}",
        "",
        "## シナリオ結果",
        "",
        "| 問題 | 種別 | 操作 | 期待 | 実際 | 結果 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for result in flow_results:
        for step in result["steps"]:
            expected = step.get("expected_verdict", "-")
            actual = step.get("actual_verdict", "hint" if step["action"] == "hint" else "-")
            ok = "PASS"
            if "matched" in step and not step["matched"]:
                ok = "FAIL"
            if step["action"] == "hint" and (not step["hint_present"] or step["answer_leak"]):
                ok = "FAIL"
            lines.append(f"| {result['id']} | {result['type']} | {step['action']} | {expected} | {actual} | {ok} |")
    lines.extend([
        "",
        "## 妥当性評価",
        "",
        f"- 5問完走: {'PASS' if validity['five_questions_completed'] else 'FAIL'}",
        f"- 出題順序: {'PASS' if validity['scenario_order_matches'] else 'FAIL'}",
        f"- 前提知識2問 + 実装説明3問: {'PASS' if validity['type_structure_valid'] else 'FAIL'} ({', '.join(validity['question_type_structure'])})",
        f"- スクリプト期待判定一致: {validity['scripted_verdicts_matched']}/{validity['scripted_verdicts_total']}",
        "",
        "## 正確性評価",
        "",
        f"- オフライン判定精度: {accuracy['passed']}/{accuracy['total']} ({accuracy['accuracy']:.0%})",
        f"- 初回正答率: {session_report['first_correct_rate']:.0%}",
        f"- 最終正答率: {session_report['final_correct_rate']:.0%}",
        f"- ヒント有効率: {session_report['hint_effective_rate']:.0%}",
        f"- 判定失敗: {json.dumps(accuracy['failures'], ensure_ascii=False)}",
        "",
        "## セキュリティ評価",
        "",
        f"- 問題表示での答え漏洩: {'PASS' if not security['public_question_answer_leaks'] else 'FAIL'}",
        f"- 未完了レスポンスでの答え漏洩: {'PASS' if not security['premature_response_answer_leaks'] else 'FAIL'}",
        f"- ヒントでの答え漏洩: {'PASS' if not security['hint_answer_leaks'] else 'FAIL'}",
        f"- ログ内の秘密情報らしき文字列: {security['secret_like_patterns_in_log']}",
        f"- 実行方式: {security['network_mode']}",
        "",
        "## セッション集計",
        "",
        f"- 試行問題数: {session_report['attempted']}",
        f"- ヒント提示数: {session_report['hints_shown']}",
        f"- 答え漏洩率: {session_report['leak_rate']:.0%}",
        f"- 解説の根拠被覆率: {session_report['groundedness']:.0%}",
        f"- 苦手傾向メモ: {', '.join(session_report['weak_topic_notes']) or 'なし'}",
        "",
        "## 不確実性・制約",
        "",
        "- FakeLLM による決定的QAであり、Gemini実APIの品質や外部検索結果の揺れは評価対象外。",
        "- VSCode Webviewの表示確認、git hook経由の実コミット連携、実ネットワーク検索は今回の自動QAには含めていない。",
    ])
    overall = all([
        validity["five_questions_completed"],
        validity["scenario_order_matches"],
        validity["type_structure_valid"],
        validity["scripted_verdicts_matched"] == validity["scripted_verdicts_total"],
        accuracy["accuracy"] == 1.0,
        not security["public_question_answer_leaks"],
        not security["premature_response_answer_leaks"],
        not security["hint_answer_leaks"],
        security["secret_like_patterns_in_log"] == 0,
    ])
    lines.insert(5, f"総合判定: {'PASS' if overall else 'FAIL'}")
    return "\n".join(lines) + "\n"


def main() -> int:
    client, session_id = start_client()
    flow_results = run_flow(client, session_id)
    api_report = client.get(f"/quiz/{session_id}/report").json()["report"]
    interactions = load_history_interactions()
    recalculated_report = build_report(interactions, completed=api_report["completed"])
    session_report = {
        "attempted": recalculated_report.attempted,
        "first_correct_rate": recalculated_report.first_correct_rate,
        "final_correct_rate": recalculated_report.final_correct_rate,
        "hints_shown": recalculated_report.hints_shown,
        "leak_rate": recalculated_report.leak_rate,
        "hint_effective_rate": recalculated_report.hint_effective_rate,
        "groundedness": recalculated_report.groundedness,
        "weak_topic_notes": recalculated_report.weak_topic_notes,
        "completed": recalculated_report.completed,
    }
    validity = build_validity_results(flow_results, session_report)
    accuracy = evaluate_accuracy()
    security = build_security_results(flow_results)
    report = render_report(flow_results, validity, accuracy, security, session_report)
    REPORT_PATH.write_text(report, encoding="utf-8")
    print(report)
    failed = "総合判定: FAIL" in report
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
