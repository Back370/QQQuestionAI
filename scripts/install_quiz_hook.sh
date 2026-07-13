#!/bin/sh
# QQQuestionAI のセットアップをコマンド一つで行う:
#   1. 対象リポジトリに pre-commit フックをインストール
#   2. シェル設定 (~/.zshrc / ~/.bashrc) に `-q` 検知ラッパーを自動追記（1回だけ）
#
# 使い方: 対象リポジトリのルートで
#   /path/to/QQQuestionAI/scripts/install_quiz_hook.sh
# ラッパーの追記をしたくない場合:
#   /path/to/QQQuestionAI/scripts/install_quiz_hook.sh --no-shell
set -e

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
HOOK_SRC="${SCRIPT_DIR}/hooks/qqquestion-pre-commit"

# --- 1. pre-commit フック -----------------------------------------------------

GIT_DIR=$(git rev-parse --git-dir 2>/dev/null) || {
    echo "エラー: git リポジトリの中で実行してください" >&2
    exit 1
}
HOOK_DST="${GIT_DIR}/hooks/pre-commit"

if [ -e "$HOOK_DST" ]; then
    if grep -q "QQQuestionAI" "$HOOK_DST" 2>/dev/null; then
        echo "[1/2] フック: 既にインストール済み ($HOOK_DST)"
    else
        echo "[1/2] フック: 既存の pre-commit フックがあります: $HOOK_DST" >&2
        echo "      上書きしないため、次の1行を既存フックに追記してください:" >&2
        echo "        sh \"$HOOK_SRC\" || exit 1" >&2
    fi
else
    mkdir -p "${GIT_DIR}/hooks"
    cp "$HOOK_SRC" "$HOOK_DST"
    chmod +x "$HOOK_DST"
    echo "[1/2] フック: インストールしました ($HOOK_DST)"
fi

# --- 2. シェルラッパー ---------------------------------------------------------

if [ "$1" = "--no-shell" ]; then
    echo "[2/2] シェルラッパー: --no-shell 指定によりスキップ"
    exit 0
fi

case "${SHELL:-}" in
    */zsh)  RC_FILE="$HOME/.zshrc" ;;
    */bash) RC_FILE="$HOME/.bashrc" ;;
    *)      RC_FILE="$HOME/.zshrc" ;;  # macOS 既定は zsh
esac

if grep -q "QQQ_QUIZ" "$RC_FILE" 2>/dev/null; then
    echo "[2/2] シェルラッパー: 設定済み ($RC_FILE)"
else
    cat >> "$RC_FILE" <<'EOS'

# >>> QQQuestionAI: git commit -q で理解度チェックを発動するラッパー >>>
git() {
    if [ "$1" = "commit" ]; then
        for arg in "$@"; do
            case "$arg" in
                -q|--quiet) QQQ_QUIZ=1 command git "$@"; return $? ;;
                --) break ;;
            esac
        done
    fi
    command git "$@"
}
# <<< QQQuestionAI <<<
EOS
    echo "[2/2] シェルラッパー: $RC_FILE に追記しました"
    echo
    echo "★ 反映するには、開いているターミナルで次を実行してください:"
    echo "    source $RC_FILE"
fi

echo
echo "セットアップ完了。git add して 'git commit -q -m \"...\"' でクイズが始まります。"
