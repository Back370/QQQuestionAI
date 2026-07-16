"""フック導入スクリプトとフック本体の安全性テスト。

ここで固定したいのは「素の git を壊さない」という性質:
- `git commit -q` は git 本来のフラグなので、クイズを発動させてはいけない
- 利用者のシェル設定 (~/.zshrc 等) を勝手に書き換えてはいけない

以前は ~/.zshrc に git() 関数を定義して `-q` を横取りしていたため、
その退行を防ぐ回帰テストとして書いている。
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL_SCRIPT = REPO_ROOT / "scripts" / "install_quiz_hook.sh"
HOOK_SRC = REPO_ROOT / "scripts" / "hooks" / "qqquestion-pre-commit"


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture()
def fake_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    # 既存のシェル設定を模して、スクリプトが書き換えないことを確認できるようにする
    (home / ".zshrc").write_text("# 利用者の既存設定\nexport FOO=bar\n", encoding="utf-8")
    (home / ".bashrc").write_text("# 利用者の既存設定\n", encoding="utf-8")
    return home


@pytest.fixture()
def git_repo(tmp_path: Path, fake_home: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(fake_home)}
    _git(repo, "init", "-q", env=env)
    _git(repo, "config", "user.email", "test@example.com", env=env)
    _git(repo, "config", "user.name", "Test", env=env)
    return repo


def _install(repo: Path, home: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["sh", str(INSTALL_SCRIPT)],
        cwd=repo,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(home), "SHELL": "/bin/zsh"},
        capture_output=True,
        text=True,
        check=False,
    )


def test_install_puts_hook_in_repo(git_repo: Path, fake_home: Path) -> None:
    result = _install(git_repo, fake_home)
    assert result.returncode == 0, result.stderr
    hook = git_repo / ".git" / "hooks" / "pre-commit"
    assert hook.exists()
    assert "QQQuestionAI" in hook.read_text(encoding="utf-8")


def test_install_does_not_touch_shell_rc(git_repo: Path, fake_home: Path) -> None:
    """シェル設定への自動追記は廃止した（git 関数の上書きは素の git を壊すため）。"""
    before = {
        name: (fake_home / name).read_text(encoding="utf-8") for name in (".zshrc", ".bashrc")
    }
    _install(git_repo, fake_home)
    for name, content in before.items():
        assert (fake_home / name).read_text(encoding="utf-8") == content, f"{name} が書き換えられた"


def test_install_script_defines_no_git_function() -> None:
    """`git()` を定義して commit を横取りする実装が復活していないこと。

    案内文で git() に言及することはあるので、行頭の関数定義だけを見る。
    """
    text = INSTALL_SCRIPT.read_text(encoding="utf-8")
    assert re.search(r"^\s*git\s*\(\)\s*\{", text, re.MULTILINE) is None
    # 本物の git へ委譲する実装（ラッパーの痕跡）も無いこと
    assert re.search(r"^\s*command git\b", text, re.MULTILINE) is None


def test_hook_is_self_contained() -> None:
    """フックはリポジトリ内のソース（backend/ 等）に依存しないこと。

    フックは対象リポジトリの .git/hooks/ に単体でコピーされる。そこから
    このプロジェクトのファイルを参照すると、コピー先では必ず壊れる。
    実行時に呼ぶのは curl / git / python3 だけであること。
    """
    # コメントと、利用者への案内文 (echo) は対象外。実際に実行される部分だけを見る。
    lines = [
        line
        for line in HOOK_SRC.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#") and not line.lstrip().startswith("echo ")
    ]
    body = "\n".join(lines)
    for path_ref in ("backend/", "scripts/", "qqquestion.server", "install_quiz_hook"):
        assert path_ref not in body, f"フック本体がリポジトリ内の {path_ref} を参照している"


@pytest.mark.parametrize("env_value", [None, "0", ""])
def test_hook_skips_without_explicit_opt_in(git_repo: Path, fake_home: Path, env_value) -> None:
    """QQQ_QUIZ=1 と明示しない限り、フックは即 exit 0（素通し）。

    これにより `git commit -q` を含む通常のコミットは一切影響を受けない。
    """
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(fake_home)}
    if env_value is not None:
        env["QQQ_QUIZ"] = env_value
    result = subprocess.run(
        ["sh", str(HOOK_SRC)],
        cwd=git_repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    # バックエンドに問い合わせすらしない（発動の気配を出さない）
    assert result.stderr.strip() == ""


@pytest.mark.skipif(shutil.which("git") is None, reason="git が必要")
def test_git_commit_quiet_is_not_intercepted(git_repo: Path, fake_home: Path) -> None:
    """`git commit -q` がフック導入後もそのままコミットできること（本丸の回帰）。"""
    _install(git_repo, fake_home)
    (git_repo / "a.txt").write_text("hello\n", encoding="utf-8")
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(fake_home)}
    _git(git_repo, "add", "a.txt", env=env)
    result = _git(git_repo, "commit", "-q", "-m", "初回", env=env)
    assert result.returncode == 0, result.stderr
    log = _git(git_repo, "log", "--oneline", env=env)
    assert "初回" in log.stdout
