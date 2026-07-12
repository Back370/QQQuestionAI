#!/usr/bin/env python3
"""安全網の整合性検査。

このテンプレート（および派生プロジェクト）の安全網が壊れていないかを検査する。
テンプレートリポジトリではCIから毎push実行され、派生プロジェクトでは
project-health-check スキルの自己点検ステップから実行される。

使い方: python3 scripts/verify_safety_net.py
標準ライブラリのみで動く（YAML検証はPyYAML→rubyの順で試し、無ければスキップ報告）。
"""
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

results = []  # (ok, label, detail)


def check(ok, label, detail=""):
    results.append((ok, label, detail))
    print(f"{'OK  ' if ok else 'FAIL'} {label}" + (f" — {detail}" if detail else ""))


def skip(label, reason):
    print(f"SKIP {label} — {reason}")


def load_jsonc(path):
    """settings.json はJSONC相当（末尾カンマ許容）なので緩くパースする。"""
    text = open(path).read()
    text = re.sub(r",(\s*[}\]])", r"\1", text)
    return json.loads(text)


# --- 1. settings.json: denyベースラインとhook設定 ---------------------------

SETTINGS = os.path.join(ROOT, ".claude", "settings.json")
REQUIRED_DENY = [
    "Bash(rm -rf:*)",
    "Bash(git push --force:*)",
    "Bash(git reset --hard:*)",
    "Bash(git clean:*)",
    "Bash(git branch -D:*)",
    "Bash(git stash clear:*)",
    "Bash(git stash drop:*)",
    "Read(**/.env)",
    "Read(**/*.pem)",
    "Read(~/.ssh/**)",
]

if os.path.exists(SETTINGS):
    try:
        settings = load_jsonc(SETTINGS)
        check(True, "settings.json がパースできる")
        deny = settings.get("permissions", {}).get("deny", [])
        missing = [r for r in REQUIRED_DENY if r not in deny]
        check(
            not missing,
            "permissions.deny にベースラインが残っている",
            f"欠落: {missing}" if missing else "",
        )
        hooks_str = json.dumps(settings.get("hooks", {}))
        check(
            "deny_dangerous_bash.py" in hooks_str,
            "PreToolUse hook (deny_dangerous_bash.py) が設定されている",
        )
    except Exception as e:
        check(False, "settings.json がパースできる", str(e))
else:
    check(False, ".claude/settings.json が存在する")

# --- 2. hook本体と回帰テスト -------------------------------------------------

HOOK = os.path.join(ROOT, ".claude", "hooks", "deny_dangerous_bash.py")
HOOK_TEST = os.path.join(ROOT, ".claude", "hooks", "test_deny_dangerous_bash.py")

check(os.path.exists(HOOK), "hookスクリプトが存在する")
if os.path.exists(HOOK_TEST):
    r = subprocess.run([sys.executable, HOOK_TEST], capture_output=True, text=True)
    check(
        r.returncode == 0,
        "hookの回帰テストが通る",
        "" if r.returncode == 0 else (r.stdout + r.stderr).strip().splitlines()[-1],
    )
else:
    check(False, "hookの回帰テストが存在する")

# --- 3. 同梱スキルの存在とfrontmatter ----------------------------------------

SKILLS_DIR = os.path.join(ROOT, ".claude", "skills")
CORE_SKILLS = ["safe-rollback", "go-live-checklist", "project-health-check"]

if os.path.isdir(SKILLS_DIR):
    for name in CORE_SKILLS:
        path = os.path.join(SKILLS_DIR, name, "SKILL.md")
        check(os.path.exists(path), f"コアスキル {name} が存在する")
    for entry in sorted(os.listdir(SKILLS_DIR)):
        path = os.path.join(SKILLS_DIR, entry, "SKILL.md")
        if not os.path.exists(path):
            continue
        head = open(path).read(2000)
        ok = head.startswith("---") and "name:" in head and "description:" in head
        check(ok, f"スキル {entry} のfrontmatterが正しい")
        index = os.path.join(SKILLS_DIR, "README.md")
        if os.path.exists(index):
            listed = entry in open(index).read()
            check(listed, f"スキル {entry} が skills/README.md の一覧に載っている")
else:
    check(False, ".claude/skills/ が存在する")

# --- 4. CI雛形のYAML/JSON構文（テンプレートにのみ存在） ------------------------


def validate_yaml(body):
    try:
        import yaml  # type: ignore

        yaml.safe_load(body)
        return True, ""
    except ImportError:
        pass
    except Exception as e:
        return False, str(e).splitlines()[0]
    if os.path.exists("/usr/bin/ruby"):
        r = subprocess.run(
            ["/usr/bin/ruby", "-ryaml", "-e", "YAML.load(STDIN.read)"],
            input=body,
            capture_output=True,
            text=True,
        )
        return r.returncode == 0, r.stderr.strip().splitlines()[-1] if r.returncode else ""
    return None, "PyYAMLもrubyも無い"


REF = os.path.join(
    ROOT, ".claude", "skills", "claude-project-setup", "references", "ci-workflow-examples.md"
)
if os.path.exists(REF):
    md = open(REF).read()
    for i, m in enumerate(re.finditer(r"```(yaml|json|bash)\n(.*?)```", md, re.S)):
        lang, body = m.group(1), m.group(2)
        label = f"ci-workflow-examples.md ブロック{i} [{lang}]"
        if lang == "yaml":
            ok, detail = validate_yaml(body)
            if ok is None:
                skip(label, detail)
            else:
                check(ok, label, detail)
        elif lang == "json":
            try:
                json.loads(body)
                check(True, label)
            except Exception as e:
                check(False, label, str(e))
        else:
            hm = re.search(r"<<'EOF'\n(.*?)\nEOF", body, re.S)
            if hm:
                try:
                    json.loads(hm.group(1))
                    check(True, label + " ヒアドキュメントJSON")
                except Exception as e:
                    check(False, label + " ヒアドキュメントJSON", str(e))
else:
    skip("CI雛形の構文検査", "参照ファイルなし（派生プロジェクトでは正常）")

# --- 4b. .github/workflows 自体の構文とSHA固定 --------------------------------
# SHA固定の強制はテンプレート自身に限定する（ci-workflow-examples.md の有無で判定）。
# 派生プロジェクトのCIは雛形からタグ参照（@v4等）で生成されるため、そちらには課さない。

WORKFLOWS_DIR = os.path.join(ROOT, ".github", "workflows")
IS_TEMPLATE = os.path.exists(REF)

if os.path.isdir(WORKFLOWS_DIR):
    for fname in sorted(os.listdir(WORKFLOWS_DIR)):
        if not fname.endswith((".yml", ".yaml")):
            continue
        body = open(os.path.join(WORKFLOWS_DIR, fname)).read()
        label = f"workflows/{fname} がYAMLとしてパースできる"
        ok, detail = validate_yaml(body)
        if ok is None:
            skip(label, detail)
        else:
            check(ok, label, detail)
        if IS_TEMPLATE:
            unpinned = [
                m.group(1)
                for m in re.finditer(r"^\s*(?:-\s+)?uses:\s*([^\s#]+)", body, re.M)
                if not m.group(1).startswith(("./", "docker://"))
                and not re.search(r"@[0-9a-f]{40}$", m.group(1))
            ]
            check(
                not unpinned,
                f"workflows/{fname} のactionがコミットSHAで固定されている",
                f"未固定: {unpinned}" if unpinned else "",
            )
else:
    skip(".github/workflows の検査", "ディレクトリなし（セットアップ前の派生プロジェクトでは正常）")

DEPENDABOT = os.path.join(ROOT, ".github", "dependabot.yml")
if os.path.exists(DEPENDABOT):
    body = open(DEPENDABOT).read()
    ok, detail = validate_yaml(body)
    if ok is None:
        skip("dependabot.yml がYAMLとしてパースできる", detail)
    else:
        check(ok, "dependabot.yml がYAMLとしてパースできる", detail)
    if IS_TEMPLATE:
        check(
            "github-actions" in body,
            "dependabot.yml が github-actions を追従している（SHA固定の更新用）",
        )
elif IS_TEMPLATE:
    check(False, ".github/dependabot.yml が存在する（SHA固定の更新をDependabotが追従）")
else:
    skip("dependabot.yml の検査", "ファイルなし（セットアップ前の派生プロジェクトでは正常）")

# --- 4c. gitleaksの誤検知対処用allowlist（secret-scan.ymlが参照する場合のみ必須） ---


def validate_toml(body):
    try:
        import tomllib  # Python 3.11+

        tomllib.loads(body)
        return True, ""
    except ModuleNotFoundError:
        return None, "tomllibが無い（Python 3.11未満）"
    except Exception as e:
        return False, str(e).splitlines()[0]


SECRET_SCAN = os.path.join(ROOT, ".github", "workflows", "secret-scan.yml")
GITLEAKS_TOML = os.path.join(ROOT, ".gitleaks.toml")
if os.path.exists(SECRET_SCAN) and ".gitleaks.toml" in open(SECRET_SCAN).read():
    check(
        os.path.exists(GITLEAKS_TOML),
        ".gitleaks.toml が存在する（secret-scan.ymlが --config で参照している）",
    )
    if os.path.exists(GITLEAKS_TOML):
        ok, detail = validate_toml(open(GITLEAKS_TOML).read())
        if ok is None:
            skip(".gitleaks.toml がTOMLとしてパースできる", detail)
        else:
            check(ok, ".gitleaks.toml がTOMLとしてパースできる", detail)

# --- 5. クロスツール指示（AGENTS.md）とCLAUDE.mdの接続 -------------------------

AGENTS_MD = os.path.join(ROOT, "AGENTS.md")
CLAUDE_MD = os.path.join(ROOT, "CLAUDE.md")

check(os.path.exists(AGENTS_MD), "AGENTS.md が存在する（Codex/Cursor等への安全ルール）")
if os.path.exists(CLAUDE_MD):
    check(
        "@AGENTS.md" in open(CLAUDE_MD).read(),
        "CLAUDE.md が @AGENTS.md をインポートしている",
    )
else:
    check(False, "CLAUDE.md が存在する")

# --- 6. Markdownの相対リンク切れ ----------------------------------------------

md_files = []
for dirpath, dirnames, filenames in os.walk(ROOT):
    dirnames[:] = [d for d in dirnames if d not in (".git", "node_modules", ".idea")]
    md_files += [os.path.join(dirpath, f) for f in filenames if f.endswith(".md")]

broken = []
for path in md_files:
    for m in re.finditer(r"\[[^\]]*\]\(([^)]+)\)", open(path).read()):
        target = m.group(1).split("#")[0].strip()
        if not target or target.startswith(("http://", "https://", "mailto:")):
            continue
        resolved = os.path.normpath(os.path.join(os.path.dirname(path), target))
        if not os.path.exists(resolved):
            broken.append(f"{os.path.relpath(path, ROOT)} → {target}")
check(not broken, "Markdownの相対リンクが全て解決できる", "; ".join(broken))

# --- 結果 ---------------------------------------------------------------------

fails = [r for r in results if not r[0]]
print(f"\n{len(results)}件中 {len(fails)}件失敗")
sys.exit(1 if fails else 0)