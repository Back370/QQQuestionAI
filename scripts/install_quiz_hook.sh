#!/bin/sh
# QQQuestionAI の pre-commit フックを対象リポジトリにインストールする。
# 既存の pre-commit フックがある場合は上書きせず追記を案内する。
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
        echo "既にインストール済みです: $HOOK_DST"
    else
        echo "既存の pre-commit フックがあります: $HOOK_DST" >&2
        echo "上書きしないため、次の1行を既存フックに追記してください:" >&2
        echo "  sh \"$HOOK_SRC\" || exit 1" >&2
        exit 1
    fi
else
    mkdir -p "${GIT_DIR}/hooks"
    cp "$HOOK_SRC" "$HOOK_DST"
    chmod +x "$HOOK_DST"
    echo "インストールしました: $HOOK_DST"
fi

cat <<'EOS'

次に、`git commit -q` で理解度チェックが発動するよう、シェルの設定
(~/.zshrc など) に以下の関数を追記してください。
git のフックは -q オプションを直接見られないため、-q を検知して
環境変数 QQQ_QUIZ=1 を立てるラッパーです。

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

追記後は `source ~/.zshrc` で反映してください。
EOS
