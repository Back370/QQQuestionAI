#!/usr/bin/env python3
"""PreToolUse hook: 破壊的なBashコマンドと秘密情報の読み取りをブロックする。

settings.json の permissions.deny は前方一致なので、フラグ後置の変種や
ラッパー経由（bash -c, env, timeout 等）の実行を検出できない。このhookは
コマンドをトークン単位で解析し、表現の変種に関係なく危険な操作をブロックする。

これはブロックリスト方式であり、シェルの全表現を列挙することは原理的に
できない。**第一関門であって最後の砦ではない**（最後の砦はCI・レビュー層）。
検出パターンを変更したら test_deny_dangerous_bash.py を必ず更新・実行すること。

標準ライブラリのみ使用。python3 がPATHに必要。exit 2 でブロック
（stderrがClaudeに伝わる）。入力が解釈できない場合はブロックしない
（フェイルオープン）。
"""
import json
import re
import sys

# コマンド名の前に付いても実体を変えないラッパー類（剥がして中身を検査する）
WRAPPERS = {
    "sudo", "doas", "env", "command", "builtin", "exec",
    "nohup", "nice", "time", "stdbuf", "timeout", "xargs",
}
# `-c` で文字列をコマンドとして実行するシェル
SHELLS = {"sh", "bash", "zsh", "dash", "ksh"}

# 秘密情報ファイルのパスパターン（Readツールのdenyと対になるBash側の検査）
SECRET_PATH = re.compile(
    r"(^|/)\.env(\.[\w.-]+)?$"          # .env, .env.production 等
    r"|(^|/)id_(rsa|dsa|ecdsa|ed25519)$"  # 秘密鍵（.pubは公開鍵なので対象外）
    r"|\.pem$"
    r"|(^|/)\.ssh(/|$)"
    r"|(^|/)\.aws(/|$)"
)
# 雛形ファイルは読んでよい（cp .env.example .env のような正当な操作を通す）
SECRET_OK = re.compile(r"\.env\.(example|sample|template|dist)$")

# 内容の読み取りに使われるコマンド（全引数が読み取り元）
READ_CMDS = {
    "cat", "head", "tail", "less", "more", "nl", "tac",
    "grep", "egrep", "fgrep", "rg", "ag",
    "awk", "sed", "cut", "sort", "uniq", "strings", "diff",
    "xxd", "od", "hexdump", "base64",
}
# 複製・転送コマンド（最後の位置引数は書き込み先なので読み取り検査から除外）。
# 「cp .env.example .env」は雛形→本物のセットアップなので通り、
# 「cp .env /tmp/backup」は本物の.envを読むのでブロックされる。
COPY_CMDS = {"cp", "scp", "rsync", "install"}


def flag_letters(tokens):
    """`-rf` や `-r -f` のような短縮フラグの文字集合（小文字化）を返す。"""
    letters = set()
    for t in tokens:
        if re.fullmatch(r"-[a-zA-Z]+", t):
            letters.update(t[1:].lower())
    return letters


def normalize_tokens(tokens):
    """先頭の環境変数代入（FOO=1 git ...）を除去し、コマンド名を
    引用符・バックスラッシュ・パス指定（/usr/bin/git 等）から正規化する。"""
    while tokens and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=\S*", tokens[0]):
        tokens = tokens[1:]
    if not tokens:
        return tokens
    cmd = tokens[0].strip("\"'").lstrip("\\").rsplit("/", 1)[-1]
    return [cmd] + tokens[1:]


def strip_wrappers(tokens):
    """sudo/env/timeout等のラッパーを剥がし、実体のコマンドに到達する。"""
    tokens = normalize_tokens(tokens)
    while tokens and tokens[0] in WRAPPERS:
        tokens = normalize_tokens(tokens[1:])
        # ラッパー自身のオプションや timeout の持続時間（5s, 1m, 2.5h 等の
        # 単位付き書式を含む）を読み飛ばす
        while tokens and (
            tokens[0].startswith("-")
            or re.fullmatch(r"\d+(\.\d+)?[smhd]?", tokens[0])
        ):
            tokens = tokens[1:]
        tokens = normalize_tokens(tokens)
    return tokens


def git_subcommand(rest):
    """gitのグローバルオプション（-C <dir>, -c <kv> 等）を読み飛ばして
    サブコマンドを返す。引数中の単語（コミットメッセージ等）に反応しない。"""
    i = 0
    while i < len(rest):
        t = rest[i]
        if t in ("-C", "-c", "--git-dir", "--work-tree", "--namespace"):
            i += 2
            continue
        if t.startswith("-"):
            i += 1
            continue
        return t, rest[i + 1:]
    return None, []


def has_opt(rest, regex):
    """曖昧でない長オプション略記・拡張（getoptが受理する `--hard` に対する
    `--ha`, `--force` に対する `--force-with-lease` 等）にマッチするトークンが
    あるか。regexはフルマッチで評価する。"""
    return any(re.fullmatch(regex, t.split("=", 1)[0]) for t in rest)


# リダイレクト演算子（2>/dev/null, >>out, <in, 2>&1, &> 等）の先頭パターン
REDIRECT = re.compile(r"\d*>>?|\d*>&\d*|&>>?|<<?")


def positional_args(tokens):
    """フラグでもリダイレクトでもない位置引数だけを返す。
    `cp a b 2>/dev/null` の 2>/dev/null や、`> file` のfileを除外する。"""
    args, skip_next = [], False
    for t in tokens:
        if skip_next:
            skip_next = False
            continue
        if t.startswith("-"):
            continue
        m = REDIRECT.match(t)
        if m:
            # 演算子だけのトークン（`> file` の `>`）は次トークンがリダイレクト先
            if m.group(0) == t and t[-1] in "<>":
                skip_next = True
            continue
        args.append(t)
    return args


def check_segment(segment):
    """パイプ・連結で区切られた1コマンド分を検査。ブロック理由 or None を返す。"""
    tokens = strip_wrappers(segment.split())
    if not tokens:
        return None

    cmd = tokens[0]
    rest = [t.strip("\"'") for t in tokens[1:]]

    # シェル経由（bash -c "..."）や eval は、引用符の中身を取り出して再帰検査する。
    # -c は -lc / -xc のように他フラグと束ねられるので、束ねフラグも拾う
    shell_c = any(re.fullmatch(r"-[a-zA-Z]*c[a-zA-Z]*", t) for t in rest)
    if (cmd in SHELLS and shell_c) or cmd == "eval":
        for m in re.finditer(r"\"([^\"]*)\"|'([^']*)'", segment):
            reason = check_command(m.group(1) or m.group(2) or "")
            if reason:
                return reason
        if cmd == "eval":
            reason = check_command(" ".join(rest))
            if reason:
                return reason

    if cmd == "rm":
        letters = flag_letters(rest)
        long_flags = set(t for t in rest if t.startswith("--"))
        recursive = "r" in letters or "--recursive" in long_flags
        force = "f" in letters or "--force" in long_flags
        if recursive and force:
            return "rm の再帰強制削除は取り返しがつかないためブロックしています。個別のファイル削除か、ゴミ箱に相当する移動（mv）を使ってください。"

    if cmd == "find" and "-delete" in rest:
        return "find -delete は一括削除で取り返しがつかないためブロックしています。対象を確認しながら個別に削除してください。"
    if cmd == "find":
        for opt in ("-exec", "-execdir", "-ok", "-okdir"):
            if opt in rest:
                reason = check_segment(" ".join(rest[rest.index(opt) + 1:]))
                if reason:
                    return reason

    if cmd == "git":
        sub, after = git_subcommand(rest)
        # 長オプションはgetoptが曖昧でない略記を受理するため、正式表記だけでなく
        # 略記（--force→--for, --hard→--ha, --delete→--del）も拾う。
        if sub == "push":
            # -f / 束ねた -fv・-vf 等 / --for / --forc / --force / --force-with-lease
            if any(re.fullmatch(r"-[A-Za-z]*f[A-Za-z]*", t) for t in rest) or has_opt(
                rest, r"--for(c(e.*)?)?"
            ):
                return "force push はリモートの履歴を書き換えるためブロックしています。取り消しは git revert で行ってください（safe-rollback スキル参照）。"
            # refspecの `+` プレフィックス（git push origin +main）も強制pushになる
            if any(t.startswith("+") for t in after if not t.startswith("-")):
                return "refspecの + プレフィックスは force push と同じ意味のためブロックしています。取り消しは git revert で行ってください。"
        if sub == "reset" and has_opt(rest, r"--har?d?"):  # --ha / --har / --hard
            return "git reset --hard は作業内容を完全に消すためブロックしています。復旧は safe-rollback スキルの手順（退避→revert）で行ってください。"
        if sub == "clean":
            return "git clean は未追跡ファイルを削除するためブロックしています。不要ファイルは個別に削除してください。"
        if sub == "branch":
            has_big_d = any(re.fullmatch(r"-[A-Za-z]*D[A-Za-z]*", t) for t in rest)
            has_delete = has_opt(rest, r"--d(e(l(e(t(e)?)?)?)?)?") or any(
                re.fullmatch(r"-[A-Za-z]*d[A-Za-z]*", t) for t in rest
            )
            has_force = has_opt(rest, r"--for(c(e.*)?)?") or any(
                re.fullmatch(r"-[A-Za-z]*f[A-Za-z]*", t) for t in rest
            )
            if has_big_d or (has_delete and has_force):
                return "ブランチの強制削除（-D）は未マージの作業を失うためブロックしています。マージ済みなら -d を使ってください。"
        if sub == "stash":
            for t in after:
                if t.startswith("-"):
                    continue
                if t in ("clear", "drop"):
                    return "git stash の clear/drop は退避した作業を消すためブロックしています。stashは残したままにしてください（safe-rollback スキル参照）。"
                break

    # 秘密情報ファイルの読み取り・複製（Readツールのdenyと対になる検査）。
    # 免除（.env.example 等の雛形）は引数ごとに個別判定する——
    # 「cat .env.example .env」のように雛形と本物を並べても本物を捕捉するため。
    if cmd in READ_CMDS or cmd in COPY_CMDS:
        sources = positional_args(rest)
        # 複製系は最後の位置引数が書き込み先なので、読み取り元から除く。
        # ただし -t/--target-directory（略記 --targ, --t 等も含む）で書き込み先を
        # フラグ指定した場合は位置引数が全てソースになるため、末尾除外をしない。
        def is_target_flag(t):
            base = t.split("=", 1)[0]
            return base == "-t" or (
                base.startswith("--t") and "--target-directory".startswith(base)
            )

        target_flag = any(is_target_flag(t) for t in rest)
        if cmd in COPY_CMDS and sources and not target_flag:
            sources = sources[:-1]
        for t in sources:
            if SECRET_OK.search(t):
                continue
            if SECRET_PATH.search(t):
                return "秘密情報ファイル（.env・秘密鍵・~/.ssh等）の読み取り・コピーはブロックしています。設定値が必要なときは、値そのものではなくキー名をユーザーに確認してください。"

    return None


def check_command(command):
    """コマンド文字列全体を検査する（シェル経由の再帰の入口にもなる）。"""
    # パイプ・;・&&・||・サブシェル・コマンド置換（$()と``）・改行の区切りごとに検査
    for segment in re.split(r"[|;&()`\n\r]+", command):
        reason = check_segment(segment)
        if reason:
            return reason
    return None


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0  # 入力が読めない場合はブロックしない（フェイルオープン）

    if data.get("tool_name") != "Bash":
        return 0

    command = (data.get("tool_input") or {}).get("command", "") or ""
    reason = check_command(command)
    if reason:
        print(reason, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())