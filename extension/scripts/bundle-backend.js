// backend の Python ソースと git フック関連スクリプトを拡張パッケージ (.vsix) に
// 同梱するためのコピースクリプト。ソースの真実は backend/ と scripts/ 側にあり、
// ここで extension/bundled/ に複製する（compile / vscode:prepublish から呼ばれる）。
//
// これにより、利用者は QQQuestionAI のリポジトリを clone しなくても、拡張を
// インストールするだけで拡張内の同梱ソースからバックエンドを起動できる。

const fs = require("fs");
const path = require("path");

const extensionDir = path.resolve(__dirname, "..");
const repoRoot = path.resolve(extensionDir, "..");
const bundledDir = path.join(extensionDir, "bundled");

// コピー対象: [コピー元, コピー先(bundled 内)]
const targets = [
  [path.join(repoRoot, "backend", "qqquestion"), path.join(bundledDir, "qqquestion")],
  [path.join(repoRoot, "scripts"), path.join(bundledDir, "scripts")],
];

// 除外するファイル/ディレクトリ名（生成物・キャッシュ・仮想環境）
const IGNORE = new Set(["__pycache__", ".venv", ".pytest_cache", ".mypy_cache"]);

function copyRecursive(src, dst) {
  const stat = fs.statSync(src);
  if (stat.isDirectory()) {
    const base = path.basename(src);
    if (IGNORE.has(base)) {
      return;
    }
    fs.mkdirSync(dst, { recursive: true });
    for (const entry of fs.readdirSync(src)) {
      copyRecursive(path.join(src, entry), path.join(dst, entry));
    }
    return;
  }
  // .pyc など生成物は同梱しない
  if (src.endsWith(".pyc")) {
    return;
  }
  fs.copyFileSync(src, dst);
}

function main() {
  // 毎回作り直して、削除されたソースが bundled に残らないようにする
  fs.rmSync(bundledDir, { recursive: true, force: true });
  for (const [src, dst] of targets) {
    if (!fs.existsSync(src)) {
      throw new Error(`バンドル元が見つかりません: ${src}`);
    }
    copyRecursive(src, dst);
  }
  console.log(`bundled backend -> ${path.relative(extensionDir, bundledDir)}`);
}

main();
