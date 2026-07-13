// Webview クイズパネル。記述式解答・ヒント要求・ギブアップを提供する。
// 模範解答は問題が終わるまでバックエンドから返ってこない設計なので、
// このパネルが答えを先に知ることはない。

import * as vscode from "vscode";
import { AnswerResponse, BackendClient, StreamEvent } from "./backendClient";

export class QuizPanel {
  private static panels = new Map<string, QuizPanel>();
  private readonly panel: vscode.WebviewPanel;
  private finished = false;

  static show(client: BackendClient, sessionId: string): void {
    if (QuizPanel.panels.has(sessionId)) {
      QuizPanel.panels.get(sessionId)!.panel.reveal();
      return;
    }
    QuizPanel.panels.set(sessionId, new QuizPanel(client, sessionId));
  }

  private constructor(
    private readonly client: BackendClient,
    private readonly sessionId: string
  ) {
    this.panel = vscode.window.createWebviewPanel(
      "qqquestionQuiz",
      "QQQuestionAI 理解度チェック",
      vscode.ViewColumn.Beside,
      { enableScripts: true }
    );
    this.panel.webview.html = renderHtml();

    this.panel.webview.onDidReceiveMessage((message) => this.handle(message));

    // パネルを閉じる = 明示的な中断 → コミット中止（architecture.md §3）
    this.panel.onDidDispose(async () => {
      QuizPanel.panels.delete(this.sessionId);
      if (!this.finished) {
        try {
          await this.client.abort(this.sessionId);
        } catch {
          // バックエンドが落ちていればフック側がタイムアウトで中止する
        }
      }
    });

    void this.loadQuestion();
  }

  private post(message: unknown): void {
    void this.panel.webview.postMessage(message);
  }

  private async loadQuestion(): Promise<void> {
    const body = await this.client.question(this.sessionId);
    if (body.question) {
      this.post({ type: "question", question: body.question });
    } else {
      await this.completeSession();
    }
  }

  private async handle(message: { type: string; answer?: string }): Promise<void> {
    try {
      switch (message.type) {
        case "answer": {
          const answer = message.answer ?? "";
          try {
            // 半二重ストリーミング: 判定理由・解説を受信した側から表示する
            await this.client.answerStream(this.sessionId, answer, (event) =>
              this.onStreamEvent(event)
            );
          } catch (error) {
            if (!isNotFound(error)) {
              throw error;
            }
            // ストリーム未対応の旧バックエンドへのフォールバック（ワンショット）
            this.postFallback(await this.client.answer(this.sessionId, answer));
          }
          break;
        }
        case "hint": {
          // ヒントは答え漏洩チェックに全文が必要なためストリーミングしない
          const body = await this.client.hint(this.sessionId);
          this.post({ type: "hint", hint: body.hint });
          break;
        }
        case "giveup": {
          try {
            await this.client.giveUpStream(this.sessionId, (event) =>
              this.onStreamEvent(event)
            );
          } catch (error) {
            if (!isNotFound(error)) {
              throw error;
            }
            this.postFallback(await this.client.giveUp(this.sessionId));
          }
          break;
        }
      }
    } catch (error) {
      this.post({ type: "error", message: String(error) });
    }
  }

  private onStreamEvent(event: StreamEvent): void {
    switch (event.event) {
      case "judgement_partial":
        this.post({ type: "stream_reason", reason: event.reason ?? "" });
        break;
      case "judgement":
        this.post({
          type: "judgement",
          judgement: event.judgement,
          question_done: event.question_done ?? false,
          model_answer: event.model_answer,
        });
        break;
      case "explanation_partial":
        this.post({ type: "stream_explanation", explanation: event.explanation ?? "" });
        break;
      case "result": {
        const { event: _name, ...result } = event;
        this.postResult(result as unknown as AnswerResponse);
        break;
      }
    }
  }

  private postFallback(result: AnswerResponse): void {
    this.post({
      type: "judgement",
      judgement: result.judgement,
      question_done: result.question_done,
      model_answer: result.model_answer,
    });
    this.postResult(result);
  }

  private postResult(result: AnswerResponse): void {
    this.post({ type: "result", result });
    if (result.status === "completed") {
      void this.completeSession();
    }
  }

  private async completeSession(): Promise<void> {
    this.finished = true;
    try {
      const report = await this.client.report(this.sessionId);
      this.post({ type: "report", rendered: report.rendered });
    } catch (error) {
      this.post({ type: "error", message: String(error) });
    }
  }
}

function isNotFound(error: unknown): boolean {
  return String(error).includes("HTTP 404");
}

function renderHtml(): string {
  // 外部リソースなしの自己完結 HTML（CSS/JS インライン）
  return /* html */ `<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<style>
  body { font-family: var(--vscode-font-family); color: var(--vscode-foreground);
         padding: 12px 16px; line-height: 1.7; }
  h2 { font-size: 1.05em; border-bottom: 1px solid var(--vscode-panel-border); padding-bottom: 6px; }
  .meta { opacity: 0.75; font-size: 0.9em; }
  pre { background: var(--vscode-textCodeBlock-background); padding: 10px;
        overflow-x: auto; border-radius: 4px; }
  textarea { width: 100%; min-height: 84px; box-sizing: border-box;
             background: var(--vscode-input-background); color: var(--vscode-input-foreground);
             border: 1px solid var(--vscode-input-border); border-radius: 4px; padding: 8px; }
  button { margin: 8px 8px 0 0; padding: 6px 14px; border: none; border-radius: 4px;
           background: var(--vscode-button-background); color: var(--vscode-button-foreground);
           cursor: pointer; }
  button.secondary { background: var(--vscode-button-secondaryBackground);
                     color: var(--vscode-button-secondaryForeground); }
  .log { margin-top: 16px; }
  .entry { margin: 10px 0; padding: 10px; border-radius: 4px;
           border-left: 3px solid var(--vscode-panel-border);
           background: var(--vscode-editorWidget-background); white-space: pre-wrap; }
  .correct { border-left-color: #3fb950; }
  .partial { border-left-color: #d29922; }
  .incorrect { border-left-color: #f85149; }
  .hint { border-left-color: #58a6ff; }
  .pending { opacity: 0.85; }
  .citation { font-size: 0.85em; opacity: 0.8; }
  button:disabled { opacity: 0.5; cursor: default; }
</style>
</head>
<body>
  <div id="question-area">
    <h2 id="title">読み込み中…</h2>
    <div class="meta" id="meta"></div>
    <p id="text"></p>
    <pre id="code" style="display:none"></pre>
    <textarea id="answer" placeholder="記述式で解答してください"></textarea>
    <div>
      <button id="submit">解答する</button>
      <button id="hint" class="secondary">ヒント</button>
      <button id="giveup" class="secondary">ギブアップ</button>
    </div>
  </div>
  <div class="log" id="log"></div>
<script>
  const vscode = acquireVsCodeApi();
  const el = (id) => document.getElementById(id);

  function addEntry(cls, text) {
    const div = document.createElement("div");
    div.className = "entry " + cls;
    div.textContent = text;
    el("log").prepend(div);
    return div;
  }

  // ストリーミング表示中のエントリ。スナップショット全文で毎回上書きする
  let liveReason = null;
  let liveExplanation = null;

  function updateLive(current, cls, text) {
    if (!current) {
      current = addEntry(cls, text);
    } else {
      current.className = "entry " + cls;
      current.textContent = text;
    }
    return current;
  }

  function setBusy(busy) {
    for (const id of ["submit", "hint", "giveup"]) {
      el(id).disabled = busy;
    }
  }

  el("submit").addEventListener("click", () => {
    const answer = el("answer").value.trim();
    if (!answer) { return; }
    setBusy(true);
    vscode.postMessage({ type: "answer", answer });
  });
  el("hint").addEventListener("click", () => {
    setBusy(true);
    vscode.postMessage({ type: "hint" });
  });
  el("giveup").addEventListener("click", () => {
    setBusy(true);
    vscode.postMessage({ type: "giveup" });
  });

  function showQuestion(question) {
    const typeLabel = question.type === "prerequisite" ? "前提知識" : "実装の説明";
    el("title").textContent =
      "【第" + question.number + "問/" + question.total + "】";
    el("meta").textContent =
      typeLabel + "・難易度" + question.difficulty + "・トピック: " + question.topic;
    el("text").textContent = question.text;
    if (question.code_snippet) {
      el("code").textContent = question.code_snippet;
      el("code").style.display = "block";
    } else {
      el("code").style.display = "none";
    }
    el("answer").value = "";
    el("answer").focus();
  }

  window.addEventListener("message", (event) => {
    const message = event.data;
    if (message.type === "question") {
      showQuestion(message.question);
    } else if (message.type === "stream_reason") {
      // 判定理由の途中経過（半二重: 受信した側から表示）
      liveReason = updateLive(liveReason, "pending", "先生> " + message.reason);
    } else if (message.type === "judgement") {
      const judgement = message.judgement;
      let cls, text;
      if (judgement.verdict === "correct") {
        cls = "correct";
        text = "先生> 正解です！🎉 " + judgement.reason;
      } else if (judgement.verdict === "partial") {
        cls = "partial";
        text = "先生> 部分的に正解です。" + judgement.reason
          + " 正解済みの部分は繰り返さなくてよいので、足りない部分だけ補足してください。";
      } else if (message.question_done) {
        cls = "incorrect";
        text = "正解は「" + (message.model_answer || "") + "」でした。";
      } else {
        cls = "incorrect";
        text = "先生> 残念、違います。「ヒント」ボタンで手がかりを出しますよ。";
      }
      updateLive(liveReason, cls, text);
      liveReason = null;
    } else if (message.type === "stream_explanation") {
      // 解説の途中経過
      liveExplanation = updateLive(
        liveExplanation, "hint pending", "----- 解説 -----\\n" + message.explanation);
    } else if (message.type === "result") {
      const result = message.result;
      if (result.explanation) {
        let text = "----- 解説 -----\\n" + result.explanation.explanation;
        if (result.explanation.citations.length) {
          text += "\\n出典:\\n" + result.explanation.citations.map((u) => "  - " + u).join("\\n");
        }
        updateLive(liveExplanation, "hint", text);
        liveExplanation = null;
      }
      if (result.next_question) {
        showQuestion(result.next_question);
      }
      setBusy(false);
    } else if (message.type === "hint") {
      let text = "先生(ヒント)> " + message.hint.hint;
      if (message.hint.citations.length) {
        text += "\\n" + message.hint.citations.map((u) => "  出典: " + u).join("\\n");
      }
      addEntry("hint", text);
      setBusy(false);
    } else if (message.type === "report") {
      el("question-area").style.display = "none";
      addEntry("correct", message.rendered + "\\n理解度チェック完走。コミットを続行します。");
    } else if (message.type === "error") {
      addEntry("incorrect", "エラー: " + message.message);
      liveReason = null;
      liveExplanation = null;
      setBusy(false);
    }
  });
</script>
</body>
</html>`;
}
