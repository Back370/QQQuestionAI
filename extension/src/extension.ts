// QQQuestionAI VSCode 拡張のエントリポイント。
// - バックエンド (python -m qqquestion.server) を自動起動
// - /quiz/pending をポーリングし、git commit -q 起点のセッションを Webview に表示
// - pre-commit フックのインストールコマンドを提供
// - Gemini の API キーを SecretStorage で預かり、バックエンドへ環境変数で渡す

import * as childProcess from "child_process";
import * as crypto from "crypto";
import * as fs from "fs";
import * as path from "path";
import * as vscode from "vscode";
import { BackendClient } from "./backendClient";
import { QuizPanel } from "./quizPanel";

const POLL_INTERVAL_MS = 1500;

// SecretStorage 上のキー名。GUI から起動した VSCode はシェルの環境変数を
// 引き継がないため、~/.zshrc の export に頼るとキーがバックエンドに届かない。
// そこで拡張が預かり、起動時に環境変数として渡す。
const API_KEY_SECRET = "qqquestion.googleApiKey";

// 拡張に同梱する Python バックエンドが必要とするコア依存。バージョンは
// bundled/requirements.txt（backend/requirements.txt の複製）に == で固定してある。
// すべて遅延 import を前提にしているので、これだけあれば FakeLLM デモも実 Gemini も
// 起動できる（chromadb/ddgs による RAG は任意。無くてもフォールバックする）。

let backendProcess: childProcess.ChildProcess | undefined;
let pollTimer: NodeJS.Timeout | undefined;
let output: vscode.OutputChannel;
// activate で設定。拡張の設置場所と、書き込み可能な永続ストレージ。
let extensionPath = "";
let globalStoragePath = "";
let secrets: vscode.SecretStorage;

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

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

// 拡張が管理する専用 venv を用意する（venv 作成 + 依存 pip install）。
// clone 不要でバックエンドを動かすための土台。venv は globalStorage に置くので
// 拡張の更新・再インストールでも残り、書き込み可能。
async function bootstrapVenv(): Promise<string> {
  const venvDir = path.join(globalStoragePath, "venv");
  const py = venvPython(venvDir);
  const marker = path.join(venvDir, ".deps-installed");
  const requirements = path.join(bundledBackendDir(), "requirements.txt");
  // requirements.txt の内容そのものを指紋として記録し、変わったときだけ入れ直す。
  // 「一度入れたら二度と入れ直さない」だと、依存の脆弱性を直して publish しても
  // 既存利用者の venv には古いバージョンが残り続け、修正が永久に届かない。
  const fingerprint = crypto
    .createHash("sha256")
    .update(fs.readFileSync(requirements))
    .digest("hex");
  if (fs.existsSync(py) && readIfExists(marker) === fingerprint) {
    return py;
  }
  await vscode.window.withProgress(
    {
      location: vscode.ProgressLocation.Notification,
      title: "QQQuestionAI: Python 環境を準備中（初回セットアップ / 依存の更新）",
      cancellable: false,
    },
    async () => {
      fs.mkdirSync(globalStoragePath, { recursive: true });
      if (!fs.existsSync(py)) {
        await run(systemPython(), ["-m", "venv", venvDir]);
      }
      await run(py, ["-m", "pip", "install", "--upgrade", "pip"]);
      await run(py, ["-m", "pip", "install", "-r", requirements]);
      fs.writeFileSync(marker, fingerprint);
    }
  );
  return py;
}

function readIfExists(file: string): string | undefined {
  try {
    return fs.readFileSync(file, "utf8").trim();
  } catch {
    return undefined;
  }
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
  // 拡張が預かっているキーを優先する（コマンドで明示的に設定されたものなので、
  // 引き継いだ環境変数より意図が新しい）。
  const storedKey = await secrets.get(API_KEY_SECRET);
  if (storedKey) {
    env.GOOGLE_API_KEY = storedKey;
  }
  if (!env.QQQ_FAKE_LLM && !env.GOOGLE_API_KEY) {
    promptForMissingApiKey(client);
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

// キーが無いと問題を生成できず「すぐ完走」に見えるだけなので、その場で促す。
function promptForMissingApiKey(client: BackendClient): void {
  const setNow = "API キーを設定";
  void vscode.window
    .showWarningMessage(
      "QQQuestionAI: Gemini の API キーが未設定のため、問題を生成できません",
      setNow
    )
    .then((choice) => {
      if (choice === setNow) {
        void setApiKey(client);
      }
    });
}

async function setApiKey(client: BackendClient): Promise<void> {
  const existing = await secrets.get(API_KEY_SECRET);
  const input = await vscode.window.showInputBox({
    title: "QQQuestionAI: Gemini の API キー",
    prompt: existing
      ? "新しいキーを貼り付けてください（空のまま Enter で保存済みのキーを削除します）"
      : "Google AI Studio (https://aistudio.google.com/apikey) で取得したキーを貼り付けてください",
    placeHolder: "AIza...",
    password: true,
    ignoreFocusOut: true,
  });
  if (input === undefined) {
    return; // Esc でキャンセル
  }
  const key = input.trim();
  if (key) {
    await secrets.store(API_KEY_SECRET, key);
    void vscode.window.showInformationMessage(
      "QQQuestionAI: API キーを保存しました。バックエンドを再起動します"
    );
  } else if (existing) {
    await secrets.delete(API_KEY_SECRET);
    void vscode.window.showInformationMessage("QQQuestionAI: 保存済みの API キーを削除しました");
  } else {
    return;
  }
  await restartBackend(client);
}

// 起動済みバックエンドは古い環境変数のまま動いているため、キーの変更後は
// プロセスごと入れ直す。
async function restartBackend(client: BackendClient): Promise<void> {
  if (!backendProcess) {
    if (await client.health()) {
      void vscode.window.showWarningMessage(
        "QQQuestionAI: このウィンドウ以外が起動したバックエンドが動いています。" +
          "新しいキーを反映するには、そのウィンドウを再読み込みしてください"
      );
      return;
    }
    await startBackend(client);
    return;
  }
  await stopBackend();
  // ポートが解放されるまで待つ（health が通るうちは startBackend が起動を省略する）
  const deadline = Date.now() + 5000;
  while (Date.now() < deadline && (await client.health())) {
    await delay(300);
  }
  await startBackend(client);
}

function stopBackend(): Promise<void> {
  const proc = backendProcess;
  if (!proc) {
    return Promise.resolve();
  }
  return new Promise((resolve) => {
    const timer = setTimeout(resolve, 5000);
    proc.once("exit", () => {
      clearTimeout(timer);
      resolve();
    });
    proc.kill();
  });
}

function startPolling(client: BackendClient): void {
  pollTimer = setInterval(async () => {
    // health は確認しない: バックエンドが落ちていれば pending がそのまま失敗し、
    // 下の catch が拾う。1.5 秒ごとにリクエストを 2 本投げるとログが埋まる
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

// ターミナル用 `quiz` コマンドの置き場所（このディレクトリだけを PATH に足す。
// venv/bin をまるごと足すと利用者の python/pip を覆い隠してしまうため）。
function quizBinDir(): string {
  return path.join(globalStoragePath, "bin");
}

// ターミナルから使う `quiz` を生成する。
//
// 中身は remote_cli（起動済みバックエンドへの HTTP クライアント）を呼ぶだけ。
// ローカルで LLM を組み立てる cli ではなく remote_cli を使うのは、API キーが
// VSCode の SecretStorage にあり、ターミナル側から読めないため。バックエンドに
// 実行を委譲することで、この shim に秘密を一切埋め込まずに済む。
async function ensureQuizShim(): Promise<string | undefined> {
  const backendDir = findBackendDir();
  if (!backendDir) {
    return undefined;
  }
  let python: string;
  try {
    python = await resolvePython(backendDir);
  } catch {
    return undefined; // Python 未準備。startBackend 側で案内済み
  }
  const binDir = quizBinDir();
  fs.mkdirSync(binDir, { recursive: true });
  const port = String(config().get<number>("port", 8756));
  const dataDir = path.join(globalStoragePath, "data");

  if (process.platform === "win32") {
    const cmdPath = path.join(binDir, "quiz.cmd");
    fs.writeFileSync(
      cmdPath,
      [
        "@echo off",
        "REM QQQuestionAI: ターミナル用 quiz コマンド（拡張が自動生成。秘密は含まない）",
        `set "PYTHONPATH=${backendDir};%PYTHONPATH%"`,
        `if not defined QQQ_PORT set "QQQ_PORT=${port}"`,
        `if not defined QQQ_DATA_DIR set "QQQ_DATA_DIR=${dataDir}"`,
        `"${python}" -m qqquestion.remote_cli %*`,
        "",
      ].join("\r\n"),
      "utf-8"
    );
    return binDir;
  }

  const shimPath = path.join(binDir, "quiz");
  fs.writeFileSync(
    shimPath,
    [
      "#!/bin/sh",
      "# QQQuestionAI: ターミナル用 quiz コマンド（拡張が自動生成）。",
      "# 起動済みバックエンドに HTTP でつなぐだけなので、API キー等の秘密は含まない。",
      `PYTHONPATH="${backendDir}\${PYTHONPATH:+:$PYTHONPATH}" \\`,
      `QQQ_PORT="\${QQQ_PORT:-${port}}" \\`,
      `QQQ_DATA_DIR="\${QQQ_DATA_DIR:-${dataDir}}" \\`,
      `exec "${python}" -m qqquestion.remote_cli "$@"`,
      "",
    ].join("\n"),
    "utf-8"
  );
  fs.chmodSync(shimPath, 0o755);
  return binDir;
}

// 統合ターミナルで `quiz` が使えるようにする（PATH に bin ディレクトリを追加）。
async function registerQuizOnPath(context: vscode.ExtensionContext): Promise<void> {
  const binDir = await ensureQuizShim();
  if (!binDir) {
    return;
  }
  const collection = context.environmentVariableCollection;
  collection.description = "QQQuestionAI: ターミナルで quiz コマンドを使えるようにします";
  collection.replace("QQQ_QUIZ_BIN", binDir);
  collection.prepend("PATH", `${binDir}${path.delimiter}`);
  output.appendLine(`quiz コマンドを用意しました: ${path.join(binDir, "quiz")}`);
}

// 外部ターミナル（VSCode の統合ターミナル以外）向けの導入案内。
async function setupTerminalQuiz(): Promise<void> {
  const binDir = await ensureQuizShim();
  if (!binDir) {
    void vscode.window.showErrorMessage(
      "QQQuestionAI: quiz コマンドを用意できませんでした（出力パネル参照）"
    );
    output.show();
    return;
  }
  const line = `export PATH="${binDir}:$PATH"`;
  const choice = await vscode.window.showInformationMessage(
    "QQQuestionAI: VSCode の統合ターミナルでは、そのまま quiz と打てば使えます。" +
      "外部のターミナルでも使うには、下の行をシェル設定に追記してください。",
    { modal: true, detail: line },
    "行をコピー"
  );
  if (choice === "行をコピー") {
    await vscode.env.clipboard.writeText(line);
    void vscode.window.showInformationMessage("コピーしました。~/.zshrc 等に貼り付けてください");
  }
}

// バックエンドが応答するまで待つ（起動直後は数秒かかる）。
async function waitForHealth(client: BackendClient, timeoutMs: number): Promise<boolean> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await client.health()) {
      return true;
    }
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  return false;
}

// クイズ対象のリポジトリを選ぶ。複数ワークスペースなら訊く。
async function pickRepoPath(): Promise<string | undefined> {
  const folders = vscode.workspace.workspaceFolders ?? [];
  if (folders.length === 0) {
    void vscode.window.showErrorMessage("QQQuestionAI: ワークスペースが開かれていません");
    return undefined;
  }
  if (folders.length === 1) {
    return folders[0].uri.fsPath;
  }
  const picked = await vscode.window.showQuickPick(
    folders.map((folder) => ({ label: folder.name, description: folder.uri.fsPath })),
    { title: "QQQuestionAI: どのリポジトリの差分から出題しますか？" }
  );
  return picked?.description;
}

// コマンド「クイズを開始」。git のコミットとは独立して動き、結果がコミットを
// 左右することはない（全問正解してもコミットはしない）。
async function startQuiz(client: BackendClient): Promise<void> {
  const repoPath = await pickRepoPath();
  if (!repoPath) {
    return;
  }
  if (!(await client.health())) {
    await startBackend(client);
    if (!(await waitForHealth(client, 120_000))) {
      output.show();
      void vscode.window.showErrorMessage(
        "QQQuestionAI: バックエンドを起動できませんでした（出力パネル参照）"
      );
      return;
    }
  }
  try {
    const body = await vscode.window.withProgress(
      {
        location: vscode.ProgressLocation.Notification,
        title: "QQQuestionAI: 差分から問題を生成中...",
        cancellable: false,
      },
      () => client.start(repoPath)
    );
    if (body.error) {
      output.appendLine(`出題時の警告: ${body.error}`);
      void vscode.window.showWarningMessage(`QQQuestionAI: ${body.error}`);
    }
    output.appendLine(`クイズ開始: ${body.session_id} (${body.files.join(", ")})`);
    QuizPanel.show(client, body.session_id);
  } catch (error) {
    const message = String(error);
    // 400 = ステージ済み差分なし。ユーザーの操作で直せるので専用の案内にする
    if (message.includes("HTTP 400")) {
      void vscode.window.showWarningMessage(
        "QQQuestionAI: ステージ済みの差分がありません。git add してから実行してください"
      );
      return;
    }
    output.appendLine(`クイズ開始に失敗: ${message}`);
    output.show();
    void vscode.window.showErrorMessage("QQQuestionAI: クイズを開始できませんでした（出力パネル参照）");
  }
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
  secrets = context.secrets;
  const client = new BackendClient(config().get<number>("port", 8756));

  context.subscriptions.push(
    vscode.commands.registerCommand("qqquestion.startQuiz", () => startQuiz(client)),
    vscode.commands.registerCommand("qqquestion.startBackend", () => startBackend(client)),
    vscode.commands.registerCommand("qqquestion.installHook", () => installHook()),
    vscode.commands.registerCommand("qqquestion.setApiKey", () => setApiKey(client)),
    vscode.commands.registerCommand("qqquestion.setupTerminalQuiz", () => setupTerminalQuiz()),
    // ポートを変えると shim に焼き込んだ値が古くなるため作り直す
    vscode.workspace.onDidChangeConfiguration((event) => {
      if (event.affectsConfiguration("qqquestion.port")) {
        void registerQuizOnPath(context);
      }
    }),
    output
  );

  void startBackend(client);
  void registerQuizOnPath(context);
  startPolling(client);
}

export function deactivate(): void {
  if (pollTimer) {
    clearInterval(pollTimer);
  }
  backendProcess?.kill();
}
