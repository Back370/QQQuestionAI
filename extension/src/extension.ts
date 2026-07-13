// QQQuestionAI VSCode 拡張のエントリポイント。
// - バックエンド (python -m qqquestion.server) を自動起動
// - /quiz/pending をポーリングし、git commit -q 起点のセッションを Webview に表示
// - pre-commit フックのインストールコマンドを提供

import * as childProcess from "child_process";
import * as fs from "fs";
import * as path from "path";
import * as vscode from "vscode";
import { BackendClient } from "./backendClient";
import { QuizPanel } from "./quizPanel";

const POLL_INTERVAL_MS = 1500;

let backendProcess: childProcess.ChildProcess | undefined;
let pollTimer: NodeJS.Timeout | undefined;
let output: vscode.OutputChannel;

function config() {
  return vscode.workspace.getConfiguration("qqquestion");
}

function findBackendDir(): string | undefined {
  for (const folder of vscode.workspace.workspaceFolders ?? []) {
    const candidate = path.join(folder.uri.fsPath, "backend");
    if (fs.existsSync(path.join(candidate, "qqquestion", "server.py"))) {
      return candidate;
    }
  }
  return undefined;
}

function resolvePython(backendDir: string | undefined): string {
  const configured = config().get<string>("pythonPath", "");
  if (configured) {
    return configured;
  }
  if (backendDir) {
    const venvPython = path.join(backendDir, ".venv", "bin", "python");
    if (fs.existsSync(venvPython)) {
      return venvPython;
    }
  }
  return "python3";
}

async function startBackend(client: BackendClient): Promise<void> {
  if (await client.health()) {
    output.appendLine("バックエンドは既に起動しています");
    return;
  }
  const backendDir = findBackendDir();
  const python = resolvePython(backendDir);
  const env: NodeJS.ProcessEnv = {
    ...process.env,
    QQQ_PORT: String(config().get<number>("port", 8756)),
  };
  if (config().get<boolean>("fakeLlm", false)) {
    env.QQQ_FAKE_LLM = "1";
  }
  backendProcess = childProcess.spawn(python, ["-m", "qqquestion.server"], {
    cwd: backendDir,
    env,
  });
  backendProcess.stdout?.on("data", (data) => output.append(String(data)));
  backendProcess.stderr?.on("data", (data) => output.append(String(data)));
  backendProcess.on("exit", (code) => {
    output.appendLine(`バックエンドが終了しました (code=${code})`);
    backendProcess = undefined;
  });
  output.appendLine(`バックエンドを起動: ${python} -m qqquestion.server (cwd=${backendDir})`);
}

function startPolling(client: BackendClient): void {
  pollTimer = setInterval(async () => {
    if (!(await client.health())) {
      return;
    }
    try {
      const body = await client.pending();
      for (const session of body.sessions) {
        output.appendLine(
          `セッション検知: ${session.session_id} (${session.files.join(", ")})`
        );
        QuizPanel.show(client, session.session_id);
        void vscode.window.showInformationMessage(
          `QQQuestionAI: コミット前の理解度チェックを開始します（全${session.total}問）`
        );
      }
    } catch {
      // ポーリング失敗は次回に任せる
    }
  }, POLL_INTERVAL_MS);
}

async function installHook(): Promise<void> {
  const folder = vscode.workspace.workspaceFolders?.[0];
  if (!folder) {
    void vscode.window.showErrorMessage("ワークスペースが開かれていません");
    return;
  }
  // このリポジトリ同梱のインストールスクリプトを対象リポジトリで実行する
  const repoRoot = findBackendDir()
    ? path.dirname(findBackendDir()!)
    : folder.uri.fsPath;
  const script = path.join(repoRoot, "scripts", "install_quiz_hook.sh");
  if (!fs.existsSync(script)) {
    void vscode.window.showErrorMessage(`インストールスクリプトが見つかりません: ${script}`);
    return;
  }
  childProcess.execFile("sh", [script], { cwd: folder.uri.fsPath }, (error, stdout, stderr) => {
    output.appendLine(stdout + stderr);
    output.show();
    if (error) {
      void vscode.window.showErrorMessage("フックのインストールに失敗しました。出力を確認してください");
    } else {
      void vscode.window.showInformationMessage(
        "セットアップ完了（フック＋シェルラッパー）。ターミナルで source ~/.zshrc を実行してから git commit -q をどうぞ"
      );
    }
  });
}

export function activate(context: vscode.ExtensionContext): void {
  output = vscode.window.createOutputChannel("QQQuestionAI");
  const client = new BackendClient(config().get<number>("port", 8756));

  context.subscriptions.push(
    vscode.commands.registerCommand("qqquestion.startBackend", () => startBackend(client)),
    vscode.commands.registerCommand("qqquestion.installHook", () => installHook()),
    output
  );

  void startBackend(client);
  startPolling(client);
}

export function deactivate(): void {
  if (pollTimer) {
    clearInterval(pollTimer);
  }
  backendProcess?.kill();
}
