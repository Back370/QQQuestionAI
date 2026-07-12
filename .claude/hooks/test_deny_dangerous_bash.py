"""deny_dangerous_bash.py の回帰テスト。

hookを変更したら必ず実行すること: python3 .claude/hooks/test_deny_dangerous_bash.py
"""
import json
import os
import subprocess
import sys

HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "deny_dangerous_bash.py")

# (コマンド, ブロックされるべきか)
CASES = [
    # --- 基本の破壊的コマンド ---
    ("git push origin main --force", True),
    ("git push origin main -f", True),
    ("cd /tmp; git push --force-with-lease", True),
    ("git reset HEAD~1 --hard", True),
    ("git reset --hard", True),
    ("git -C /tmp/foo clean -fd", True),
    ("rm -r -f somedir", True),
    ("sudo rm --recursive --force /", True),
    ("echo hello; rm -fR dir", True),
    ("git push --force", True),
    # --- 変種（改行・env代入前置・フルパス・バッククォート・refspecの+） ---
    ("echo safe\ngit push --force", True),
    ("FOO=1 git push -f origin main", True),
    ("/usr/bin/git reset HEAD --hard", True),
    ("result=`git push --force`", True),
    ("git push origin +main:main", True),
    ("GIT_DIR=/x /opt/homebrew/bin/git clean -fd", True),
    # --- ラッパー経由のバイパス（セキュリティレビュー指摘） ---
    ('bash -c "git push --force"', True),
    ("sh -c 'rm -rf /'", True),
    ('eval "git push --force"', True),
    ("eval git reset --hard", True),
    ("env git push --force origin main", True),
    ("command git reset --hard", True),
    ("timeout 5 git push --force", True),
    # 再評価で見つかった取りこぼし（timeoutの単位付き秒数・シェル束ねフラグ・
    # 雛形と本物を並べた秘密読み取り）
    ("timeout 5s rm -rf dir", True),
    ("timeout 1.5m git push --force", True),
    ('bash -lc "git push --force"', True),
    ("sh -cx 'rm -rf /'", True),
    ("cat .env.example .env", True),
    ("grep X .env .env.sample", True),
    ("find . -name '*.log' | xargs rm -rf", True),
    ("find . -name '*.tmp' -delete", True),
    ("find . -type d -exec rm -rf {} +", True),
    # --- denyのみだった弱いルールのhook化（branch -D / stash clear|drop） ---
    ("git branch -D main", True),
    ("git branch --delete --force main", True),
    ("cd /tmp; git branch -D main", True),
    ("git stash clear", True),
    ("cd /tmp; git stash drop", True),
    # --- 秘密情報のBash経由読み取り（セキュリティレビュー指摘） ---
    ("cat .env", True),
    ("grep KEY .env.production", True),
    ("cat ~/.ssh/id_rsa", True),
    ("head config/.env.local", True),
    ("cp .env /tmp/backup", True),
    ("base64 .env", True),
    ("tac .env", True),
    ("diff .env .env.old", True),
    # 書き込み先をフラグ指定する複製（-t）でソースの.envが漏れないこと
    ("cp -t /tmp/backup .env", True),
    ("cp --target-directory=/tmp/backup .env", True),
    ("cp --target-directory /tmp/backup .env", True),
    ("install -m600 .env /tmp/backup", True),
    # 長オプション略記でのバイパス（getoptが受理する曖昧でない短縮形）
    ("cp --target destdir .env", True),
    ("cp --targ destdir .env", True),
    ("git reset --ha", True),
    ("git reset --har", True),
    ("git branch --del --force main", True),
    ("git branch --d --force main", True),
    ("git branch --de --force main", True),
    ("git push --for origin main", True),
    ("git push -fv origin main", True),
    ("git push -vf origin main", True),
    # --- 通常の開発コマンドはブロックしないこと ---
    ("git status", False),
    ("git push origin main", False),
    ("git push origin main:main", False),
    ("git reset --soft HEAD~1", False),
    ("rm -f single-file.txt", False),
    ("rm -r somedir", False),
    ("npm test", False),
    ("FOO=1 npm test", False),
    ("ls -la", False),
    ("git log --oneline -5", False),
    ("echo build\nnpm run lint", False),
    ('bash -c "npm test"', False),
    ("find . -name '*.md'", False),
    # --- 誤検知の再発防止（セキュリティレビュー指摘） ---
    ('git commit -m "clean up temp files"', False),
    ("git checkout clean", False),
    ('git commit -m "force push is banned"', False),
    ("git branch -d merged-branch", False),
    ('git stash push -m "drop the old idea"', False),
    ("git stash list", False),
    ("cat .env.example", False),
    ("cp .env.example .env", False),
    ("cp -t /tmp/dst config.json", False),
    ("cp -r srcdir destdir", False),
    ("cp app.js .env 2>/dev/null", False),
    ("git reset --help", False),
    ("git push --follow-tags origin main", False),
    ("git push -v origin main", False),
    ("git push -u origin feature", False),
    ("cat notes-target-directory.txt", False),
    ("cat README.md", False),
    ("grep -r TODO src/", False),
]


def run_hook(payload):
    return subprocess.run(
        [sys.executable, HOOK], input=payload, capture_output=True, text=True
    ).returncode


failures = 0
for cmd, expect_blocked in CASES:
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}})
    blocked = run_hook(payload) == 2
    ok = blocked == expect_blocked
    if not ok:
        failures += 1
    label = cmd.replace("\n", "⏎")
    print(f"{'OK  ' if ok else 'FAIL'} blocked={blocked} expected={expect_blocked}  {label}")

# Bash以外のツールは素通りすること
code = run_hook(json.dumps({"tool_name": "Read", "tool_input": {"file_path": "/x"}}))
print(f"{'OK  ' if code == 0 else 'FAIL'} non-Bash tool exit={code}")
failures += 0 if code == 0 else 1

# 入力が壊れていてもブロックしない（フェイルオープン）こと
code = run_hook("not json")
print(f"{'OK  ' if code == 0 else 'FAIL'} malformed input exit={code}")
failures += 0 if code == 0 else 1

print(f"\n{failures} failures")
sys.exit(1 if failures else 0)