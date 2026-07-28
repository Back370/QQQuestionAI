"""モデル切り替え（issue #20）のテスト。

環境変数 QQQ_MODEL を自分で設定しなくてもモデルを選べるようにした部分:

- 一覧は Google の ListModels から取り、失敗しても内蔵候補にフォールバックする
  （固定リストは必ず陳腐化する。このリポジトリも 2.0→2.5→3.5 と踏んでいる）
- 切り替えはプロセス全体に即時反映する。GeminiLLM は呼び出しのたびに現在の
  モデル名を読むので、バックエンドを再起動しなくても次の生成から効く
- サーバは一覧 (GET /models) と切り替え (POST /models/select) を公開する
"""

import json

import pytest
from fastapi.testclient import TestClient

from qqquestion import llm as llm_module
from qqquestion.diff_analyzer import analyze
from qqquestion.knowledge_base import InMemoryKnowledgeBase
from qqquestion.llm import (
    DEFAULT_MODEL,
    GeminiLLM,
    available_models,
    current_model_name,
    fallback_models,
    set_current_model,
)
from qqquestion.server import AppDeps, create_app

from .conftest import SAMPLE_DIFF


@pytest.fixture(autouse=True)
def clean_model_env(monkeypatch):
    """モデル関連の環境変数とキャッシュを毎回まっさらにする。"""
    monkeypatch.delenv("QQQ_MODEL", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setattr(llm_module, "_model_cache", None)
    yield
    monkeypatch.setattr(llm_module, "_model_cache", None)


# API のモデル一覧応答（実際の形に合わせた抜粋）
API_PAYLOAD_MODELS = [
    {
        "name": "models/gemini-2.5-flash",
        "displayName": "Gemini 2.5 Flash",
        "description": "高速なモデル",
        "supportedGenerationMethods": ["generateContent", "countTokens"],
    },
    {
        "name": "models/gemini-3.5-flash",
        "displayName": "Gemini 3.5 Flash",
        "description": "もっと新しい高速なモデル",
        "supportedGenerationMethods": ["generateContent"],
    },
    {
        "name": "models/text-embedding-004",
        "displayName": "Embedding",
        "supportedGenerationMethods": ["embedContent"],
    },
    {
        "name": "models/gemini-2.5-flash-tts",
        "displayName": "TTS",
        "supportedGenerationMethods": ["generateContent"],
    },
    {
        "name": "models/gemma-3-27b-it",
        "displayName": "Gemma",
        "supportedGenerationMethods": ["generateContent"],
    },
]


# ---- 現在のモデルと切り替え -------------------------------------------


def test_current_model_falls_back_to_default(monkeypatch):
    assert current_model_name() == DEFAULT_MODEL
    monkeypatch.setenv("QQQ_MODEL", "")  # 空文字も「未設定」と同じ扱いにする
    assert current_model_name() == DEFAULT_MODEL


def test_set_current_model_sets_and_clears():
    assert set_current_model("gemini-9.9-flash") == "gemini-9.9-flash"
    assert current_model_name() == "gemini-9.9-flash"
    # 空 / None は既定に戻す
    assert set_current_model("  ") == DEFAULT_MODEL
    assert set_current_model(None) == DEFAULT_MODEL


def test_gemini_reads_model_at_call_time():
    """切り替えのたびにプロセスを作り直さなくて済むよう、呼び出し時に解決する。"""
    llm = GeminiLLM()
    assert llm._model_name == DEFAULT_MODEL
    set_current_model("gemini-9.9-pro")
    assert llm._model_name == "gemini-9.9-pro"  # 既存インスタンスにも効く
    # 明示指定した場合は環境変数に左右されない（評価スクリプト等での固定用）
    assert GeminiLLM("gemini-1.0-fixed")._model_name == "gemini-1.0-fixed"


# ---- 一覧の取得 -------------------------------------------------------


class _FakeResponse:
    """urlopen の戻り値（コンテキストマネージャ + read）の最小実装。"""

    def __init__(self, payload: dict):
        self._data = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *args) -> bool:
        return False


@pytest.fixture
def fake_list_api(monkeypatch):
    """ListModels の HTTP だけを差し替える（絞り込み・整形は本物を通す）。"""
    import urllib.request

    calls: list[str] = []

    def fake_urlopen(url, timeout=None):
        calls.append(url)
        return _FakeResponse({"models": API_PAYLOAD_MODELS})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return calls


def test_available_models_uses_api_and_filters(monkeypatch, fake_list_api):
    monkeypatch.setenv("GOOGLE_API_KEY", "dummy-key")
    entries, source = available_models()
    assert source == "api"
    names = [entry["name"] for entry in entries]
    # models/ 接頭辞を外し、新しい世代を上にする
    assert names == ["gemini-3.5-flash", "gemini-2.5-flash"]
    # 埋め込み・TTS・Gemma は structured output に使えないので候補に出さない
    assert not any("embedding" in name or "tts" in name or "gemma" in name for name in names)
    assert entries[0]["label"] == "Gemini 3.5 Flash"
    assert entries[0]["description"] == "もっと新しい高速なモデル"
    assert "key=dummy-key" in fake_list_api[0]


def test_available_models_without_key_falls_back():
    entries, source = available_models()
    assert source == "fallback"
    assert DEFAULT_MODEL in [entry["name"] for entry in entries]


def test_available_models_falls_back_when_api_fails(monkeypatch, caplog):
    monkeypatch.setenv("GOOGLE_API_KEY", "secret-key-value")

    def boom(api_key: str):
        raise RuntimeError(f"403 error for https://example.com/models?key={api_key}")

    monkeypatch.setattr(llm_module, "_fetch_models", boom)
    with caplog.at_level("WARNING"):
        entries, source = available_models()
    assert source == "fallback"
    assert entries  # 切り替え UI が空にならない
    # 失敗ログに API キーを載せない（AGENTS.md 安全ルール2）
    assert "secret-key-value" not in caplog.text


def test_fallback_always_includes_current_model():
    set_current_model("gemini-my-custom")
    names = [entry["name"] for entry in fallback_models()]
    assert "gemini-my-custom" in names  # 自分で入れた名前が一覧から消えない
    assert DEFAULT_MODEL in names


def test_available_models_caches_api_result(monkeypatch, fake_list_api):
    monkeypatch.setenv("GOOGLE_API_KEY", "dummy-key")
    calls = fake_list_api
    available_models()
    available_models()
    assert len(calls) == 1  # 2回目はキャッシュ
    available_models(refresh=True)
    assert len(calls) == 2  # 明示的な更新は叩き直す
    monkeypatch.setenv("GOOGLE_API_KEY", "another-key")
    available_models()
    assert len(calls) == 3  # キーが変わったら取り直す


# ---- サーバ API -------------------------------------------------------


@pytest.fixture
def client(tmp_path, fake_llm):
    deps = AppDeps(
        llm=fake_llm,
        kb=InMemoryKnowledgeBase(),
        data_dir=tmp_path,
        diff_provider=lambda repo: analyze(SAMPLE_DIFF),
        run_in_background=lambda task: task(),
    )
    return TestClient(create_app(deps))


def test_models_endpoint_lists_choices(client):
    body = client.get("/models").json()
    assert body["current"] == DEFAULT_MODEL
    assert body["default"] == DEFAULT_MODEL
    assert body["source"] == "fallback"  # キー未設定なので内蔵候補
    assert body["models"]


def test_select_model_switches_without_restart(client):
    body = client.post("/models/select", json={"model": "gemini-9.9-flash"}).json()
    assert body["current"] == "gemini-9.9-flash"
    assert body["previous"] == DEFAULT_MODEL
    # 同じプロセスの他の経路にも効く（再起動不要）
    assert current_model_name() == "gemini-9.9-flash"
    assert client.get("/health").json()["model"] == "gemini-9.9-flash"
    assert client.get("/models").json()["current"] == "gemini-9.9-flash"


def test_select_empty_model_restores_default(client):
    client.post("/models/select", json={"model": "gemini-9.9-flash"})
    assert client.post("/models/select", json={"model": ""}).json()["current"] == DEFAULT_MODEL
    assert client.post("/models/select", json={}).json()["current"] == DEFAULT_MODEL


def test_cli_list_models_marks_current(capsys):
    """`quiz --list-models` は現在のモデルに * を付ける（キー無しでも一覧を出す）。"""
    from qqquestion.cli import print_models

    set_current_model(DEFAULT_MODEL)
    print_models()
    out = capsys.readouterr().out
    assert f" * {DEFAULT_MODEL}" in out
    assert "内蔵の候補" in out  # 一覧を取れなかったことを黙らない
    assert "--model" in out  # 切り替え方を出す


def test_start_response_reports_model(client):
    body = client.post("/quiz/start", json={"repo_path": "."}).json()
    assert body["model"] == DEFAULT_MODEL  # ターミナル/UI が「どのモデルか」を出せる
