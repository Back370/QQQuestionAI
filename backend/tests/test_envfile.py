import os

from qqquestion.envfile import load_env_file


def test_loads_keys_without_overwriting(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(
        "# コメント行\n"
        "GOOGLE_API_KEY=abc123\n"
        'TAVILY_API_KEY="quoted-value"\n'
        "export QQQ_MODEL=gemini-2.0-flash\n"
        "\n"
        "壊れた行だけどイコールなし\n"
    )
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setenv("QQQ_MODEL", "already-set")

    loaded = load_env_file(env)

    assert set(loaded) == {"GOOGLE_API_KEY", "TAVILY_API_KEY"}
    assert os.environ["GOOGLE_API_KEY"] == "abc123"
    assert os.environ["TAVILY_API_KEY"] == "quoted-value"
    assert os.environ["QQQ_MODEL"] == "already-set"  # 既存値を上書きしない


def test_missing_file_is_noop(tmp_path):
    assert load_env_file(tmp_path / "nope.env") == []


def test_qqq_env_file_override(tmp_path, monkeypatch):
    env = tmp_path / "custom.env"
    env.write_text("QQQ_TEST_KEY=hello\n")
    monkeypatch.setenv("QQQ_ENV_FILE", str(env))
    monkeypatch.delenv("QQQ_TEST_KEY", raising=False)
    assert load_env_file() == ["QQQ_TEST_KEY"]
    assert os.environ["QQQ_TEST_KEY"] == "hello"
