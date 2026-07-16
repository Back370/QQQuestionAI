#!/bin/sh
# QQQuestionAI: コミットをゲートしたい人向けの「任意」のセットアップ。
# 対象リポジトリに pre-commit フックをインストールする。
#
# 通常はこのフックは不要で、`quiz` コマンド（または VSCode の
# 「QQQuestionAI: クイズを開始」）を実行すればいつでも理解度チェックができる。
# このフックは「コミット前に必ず問われる」強制力が欲しい場合だけ入れる。
#
# 発動は明示的な指定のときだけ:
#   QQQ_QUIZ=1 git commit -m "..."
# 素の `git commit` や `git commit -q` は一切影響を受けない。
#
# 使い方: 対象リポジトリのルートで
#   /path/to/QQQuestionAI/scripts/install_quiz_hook.sh
set -e

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
HOOK_SRC="${SCRIPT_DIR}/hooks/qqquestion-pre-commit"

GIT_DIR=$(git rev-parse --git-dir 2>/dev/null) || {
    echo "エラー: git リポジトリの中で実行してください" >&2
    exit 1
}
HOOK_DST="${GIT_DIR}/hooks/pre-commit"

if [ -e "$HOOK_DST" ]; then
    if grep -q "QQQuestionAI" "$HOOK_DST" 2>/dev/null; then
        echo "フック: 既にインストール済み ($HOOK_DST)"
    else
        echo "フック: 既存の pre-commit フックがあります: $HOOK_DST" >&2
        echo "      上書きしないため、次の1行を既存フックに追記してください:" >&2
        echo "        sh \"$HOOK_SRC\" || exit 1" >&2
    fi
else
    mkdir -p "${GIT_DIR}/hooks"
    cp "$HOOK_SRC" "$HOOK_DST"
    chmod +x "$HOOK_DST"
    echo "フック: インストールしました ($HOOK_DST)"
fi

# --- 旧バージョンのシェルラッパーの検出 ---------------------------------------
# v0.2.0 以前は ~/.zshrc に git 関数を定義して `git commit -q` を横取りしていた。
# -q は git 本来のフラグ（出力抑制）で、素の git の挙動を変えてしまうため廃止した。
# 自動で書き換えると利用者のシェル設定を壊しかねないので、案内だけ出す。
for RC_FILE in "$HOME/.zshrc" "$HOME/.bashrc"; do
    if grep -q "QQQuestionAI" "$RC_FILE" 2>/dev/null; then
        echo
        echo "★ 古い設定が $RC_FILE に残っています。"
        echo "  '# >>> QQQuestionAI:' から '# <<< QQQuestionAI <<<' までのブロック"
        echo "  （git() 関数の定義）を削除してください。これは git commit -q を"
        echo "  横取りする実装で、現在は使いません。削除後 source $RC_FILE を実行。"
    fi
done

echo
echo "セットアップ完了。"
echo "  ふだんの確認   : quiz                （コミットしません）"
echo "  コミットをゲート: QQQ_QUIZ=1 git commit -m \"...\""
