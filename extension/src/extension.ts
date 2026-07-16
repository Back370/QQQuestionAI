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

// 拡張に同梱する Python バックエンドが必要とするコア依存。すべて遅延 import を
// 前提にしているので、これだけあれば FakeLLM デモも実 Gemini も起動できる
// （chromadb/ddgs による RAG は任意。無くてもフォールバックする）。
const PY_DEPS = [
  "fastapi>=0.110",
  "uvicorn>=0.29",
  "pydantic>=2.7",
  "httpx>=0.27",
  "langchain-core>=0.2",
  "langchain-google-genai>=1.0",
];

let backendProcess: childProcess.ChildProcess | undefined;
let pollTimer: NodeJS.Timeout | undefined;
let output: vscode.OutputChannel;
// activate で設定。拡張の設置場所と、書き込み可能な永続ストレージ。
let extensionPath = "";
let globalStoragePath = "";

function config() {
  return vscode.workspace.getConfiguration("qqquestion");
}

// バックエンドの Python ソースがある場所を返す。
// 1) 開発時: ワークスペースに backend/ がある（リポジトリ自身を開いているケース）
// 2) 通常: 拡張に同梱した bundled/（リポジトリを clone していないケース）
function bundledBackendDir(): string {
  return path.join(extensionPath, "bundled");
}

function findBackendDir(): string | undefined {
  for (const folder of vscode.workspace.workspaceFolders ?? []) {
    const candidate = path.join(folder.uri.fsPath, "backend");
    if (fs.existsSync(path.join(candidate, "qqquestion", "server.py"))) {
      return candidate;
    }
  }
  const bundled = bundledBackendDir();
  if (fs.existsSync(path.join(bundled, "qqquestion", "server.py"))) {
    return bundled;
  }
  return undefined;
}

// OS ごとの venv 内 python の場所。
function venvPython(venvDir: string): string {
  return process.platform === "win32"
    ? path.join(venvDir, "Scripts", "python.exe")
    : path.join(venvDir, "bin", "python");
}

// execFile を Promise 化し、出力をログに流す小ヘルパ。
function run(cmd: string, args: string[]): Promise<void> {
  return new Promise((resolve, reject) => {
    output.appendLine(`$ ${cmd} ${args.join(" ")}`);
    const proc = childProcess.execFile(cmd, args, { maxBuffer: 32 * 1024 * 1024 }, (error, stdout, stderr) => {
      if (stdout) {
        output.append(String(stdout));
      }
      if (stderr) {
        output.append(String(stderr));
      }
      if (error) {
        reject(error);
      } else {
        resolve();
      }
    });
    proc.on("error", reject);
  });
}

// venv 作成に使うシステム Python を探す。設定 > python3 > python の順。
function systemPython(): string {
  const configured = config().get<string>("pythonPath", "");
  return configured || (process.platform === "win32" ? "python" : "python3");
}

// 拡張が管理する専用 venv を用意する（初回のみ venv 作成 + 依存 pip install）。
// clone 不要でバックエンドを動かすための土台。venv は globalStorage に置くので
// 拡張の更新・再インストールでも残り、書き込み可能。
async function bootstrapVenv(): Promise<string> {
  const venvDir = path.join(globalStoragePath, "venv");
  const py = venvPython(venvDir);
  const marker = path.join(venvDir, ".deps-installed");
  if (fs.existsSync(py) && fs.existsSync(marker)) {
    return py;
  }
  await vscode.window.withProgress(
    {
      location: vscode.ProgressLocation.Notification,
      title: "QQQuestionAI: 初回セットアップ（Python 環境を準備中）",
      cancellable: false,
    },
    async () => {
      fs.mkdirSync(globalStoragePath, { recursive: true });
      if (!fs.existsSync(py)) {
        await run(systemPython(), ["-m", "venv", venvDir]);
      }
      await run(py, ["-m", "pip", "install", "--upgrade", "pip"]);
      await run(py, ["-m", "pip", "install", ...PY_DEPS]);
      fs.writeFileSync(marker, new Date().toISOString());
    }
  );
  return py;
}

// バックエンド起動に使う Python を決める。
// 設定 > ワークスペースの backend/.venv > 拡張管理の専用 venv（自動作成）。
async function resolvePython(backendDir: string | undefined): Promise<string> {
  const configured = config().get<string>("pythonPath", "");
  if (configured) {
    return configured;
  }
  if (backendDir) {
    const wsVenv = venvPython(path.join(backendDir, ".venv"));
    if (fs.existsSync(wsVenv)) {
      return wsVenv;
    }
  }
  return bootstrapVenv();
}

async function startBackend(client: BackendClient): Promise<void> {
  if (await client.health()) {
    output.appendLine("バックエンドは既に起動しています");
    return;
  }
  const backendDir = findBackendDir();
  if (!backendDir) {
    output.appendLine("バックエンドのソースが見つかりません（拡張の同梱が壊れている可能性）");
    void vscode.window.showErrorMessage(
      "QQQuestionAI: バックエンドのソースが見つかりませんでした。拡張を再インストールしてください"
    );
    return;
  }
  let python: string;
  try {
    python = await resolvePython(backendDir);
  } catch (error) {
    output.appendLine(`Python 環境の準備に失敗しました: ${String(error)}`);
    output.show();
    void vscode.window.showErrorMessage(
      "QQQuestionAI: Python 環境の準備に失敗しました。Python 3.11+ が入っているか確認するか、設定 qqquestion.pythonPath で使う Python を指定してください（出力パネル参照）"
    );
    return;
  }
  const env: NodeJS.ProcessEnv = {
    ...process.env,
    QQQ_PORT: String(config().get<number>("port", 8756)),
    // qqquestion パッケージをインポート可能にする（同梱ソースは pip install
    // していないため、cwd に加えて PYTHONPATH でも通す）。
    PYTHONPATH: [backendDir, process.env.PYTHONPATH].filter(Boolean).join(path.delimiter),
  };
  if (config().get<boolean>("fakeLlm", false)) {
    env.QQQ_FAKE_LLM = "1";
  }
  // 同梱ソースから起動する場合、cwd(=bundled) は拡張更新で消えるため、履歴や
  // ログの保存先を書き込み可能な永続ストレージに固定する。
  if (backendDir === bundledBackendDir()) {
    const dataDir = path.join(globalStoragePath, "data");
    fs.mkdirSync(dataDir, { recursive: true });
    env.QQQ_DATA_DIR = dataDir;
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
      // 自ウィンドウのワークスペースを渡す。バックエンドはコミットが走った
      // リポジトリと一致するセッションだけ返すので、別ウィンドウにパネルが
      // 開くことがなくなる
      const workspaces = (vscode.workspace.workspaceFolders ?? []).map(
        (folder) => folder.uri.fsPath
      );
      const body = await client.pending(workspaces);
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
  // フック導入スクリプトを探す。ワークスペースの backend/ 隣（開発時）か、
  // 拡張に同梱した bundled/scripts（clone していない通常ケース）。
  const backendDir = findBackendDir();
  const candidates: string[] = [];
  if (backendDir) {
    // 開発時: <repo>/scripts
    candidates.push(path.join(path.dirname(backendDir), "scripts", "install_quiz_hook.sh"));
    // 同梱時: <extension>/bundled/scripts
    candidates.push(path.join(backendDir, "scripts", "install_quiz_hook.sh"));
  }
  const script = candidates.find((candidate) => fs.existsSync(candidate));
  if (!script) {
    void vscode.window.showErrorMessage(
      `インストールスクリプトが見つかりません（探索先: ${candidates.join(", ") || "なし"}）`
    );
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
  extensionPath = context.extensionPath;
  globalStoragePath = context.globalStorageUri.fsPath;
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
