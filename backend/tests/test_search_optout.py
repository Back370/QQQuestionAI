"""Web検索の既定ONと、利用者が止められることの担保。

`ddgs` を配布物に入れた（requirements.txt）ことで、検索は全利用者で既定 ON に
なった＝差分から抽出したトピック名が DuckDuckGo に送られる。off の口は
QQQ_NO_SEARCH 環境変数しか無いが、GUI から起動した VSCode はシェルの環境変数を
引き継がないため、拡張が設定から渡さないと利用者に止める手段が無くなる。

ここで固定したいのは「検索は止められる」という性質:
- バックエンド: QQQ_NO_SEARCH=1 なら検索プロバイダを作らない
- 拡張: 設定 qqquestion.webSearch が存在し、off のとき QQQ_NO_SEARCH を渡す
- 配布物: ddgs が入っている（入っていないと表の記述が実態と食い違う）
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from qqquestion.knowledge_base import DuckDuckGoSearch, TavilySearch, create_search_provider

REPO_ROOT = Path(__file__).resolve().parents[2]
REQUIREMENTS = REPO_ROOT / "backend" / "requirements.txt"
EXTENSION_SRC = REPO_ROOT / "extension" / "src" / "extension.ts"
EXTENSION_PKG = REPO_ROOT / "extension" / "package.json"


# ---- バックエンド: 検索を止められる ----------------------------------


def test_no_search_env_disables_provider(monkeypatch):
    """QQQ_NO_SEARCH=1 は、キーやライブラリの有無に関わらず検索を止める。"""
    monkeypatch.setenv("QQQ_NO_SEARCH", "1")
    monkeypatch.setenv("TAVILY_API_KEY", "dummy-key")  # キーがあっても優先されない
    assert create_search_provider() is None


def test_default_uses_duckduckgo(monkeypatch):
    """既定（キー無し）では ddgs による DuckDuckGo 検索になる。"""
    monkeypatch.delenv("QQQ_NO_SEARCH", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    assert isinstance(create_search_provider(), DuckDuckGoSearch)


def test_tavily_key_takes_precedence(monkeypatch):
    monkeypatch.delenv("QQQ_NO_SEARCH", raising=False)
    monkeypatch.setenv("TAVILY_API_KEY", "dummy-key")
    assert isinstance(create_search_provider(), TavilySearch)


# ---- 送信先が DuckDuckGo だけに固定されている -------------------------


def test_duckduckgo_search_pins_backend_to_one_engine():
    """ddgs の既定 backend="auto" は送信先が実行のたびに変わる。

    auto は ["wikipedia", "grokipedia"] + shuffle(残り) の順に必要件数が集まるまで
    エンジンを呼ぶため、送信先が実行ごとに変動し（brave のときも yandex のときも
    ある）、README の「外部に送信されるデータ」に確定した送信先を書けない。加えて
    先頭が wikipedia/grokipedia なので、クラス名に反し DuckDuckGo にほぼ到達しない。
    送信先を名前どおり1つに保つため backend の明示を固定する。
    """
    assert DuckDuckGoSearch.BACKEND == "duckduckgo"

    captured = {}

    class _FakeDDGS:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def text(self, query, **kwargs):
            captured.update(kwargs)
            return []

    import sys
    import types

    fake_module = types.ModuleType("ddgs")
    fake_module.DDGS = _FakeDDGS
    original = sys.modules.get("ddgs")
    sys.modules["ddgs"] = fake_module
    try:
        DuckDuckGoSearch().search("RNN とは")
    finally:
        if original is None:
            del sys.modules["ddgs"]
        else:
            sys.modules["ddgs"] = original

    assert captured.get("backend") == "duckduckgo", "backend を明示せず auto に委ねている"


def test_duckduckgo_backend_name_exists_in_installed_ddgs():
    """固定した backend 名が実在すること。

    存在しない名前を渡すと ddgs は警告を出してエンジン0件になり、検索が黙って
    空を返す（KnowledgeBaseBuilder が例外を握り潰すため気付けない）。
    """
    pytest.importorskip("ddgs")
    from ddgs.ddgs import ENGINES

    assert DuckDuckGoSearch.BACKEND in ENGINES["text"]


# ---- 配布物に ddgs が入っている ---------------------------------------


def test_ddgs_is_pinned_in_requirements():
    """ddgs が配布物から抜けると、DuckDuckGo 検索は黙って起きなくなる。

    README の「外部に送信されるデータ」表は既定で検索が起きる前提で書いてある
    ので、抜けると記述が実態より過剰申告になる（逆方向の食い違い）。
    """
    text = REQUIREMENTS.read_text(encoding="utf-8")
    assert re.search(r"^ddgs==", text, re.MULTILINE), "ddgs が == で固定されていない"


# ---- 拡張: 設定から off にできる --------------------------------------


def test_extension_exposes_web_search_setting():
    """設定が無いと、GUI 起動の利用者には検索を止める手段が無い。"""
    pkg = json.loads(EXTENSION_PKG.read_text(encoding="utf-8"))
    props = pkg["contributes"]["configuration"]["properties"]
    assert "qqquestion.webSearch" in props
    setting = props["qqquestion.webSearch"]
    assert setting["type"] == "boolean"
    assert setting["default"] is True  # 既定 ON（実態と一致させる）


def test_extension_passes_no_search_when_setting_is_off():
    """設定 off が QQQ_NO_SEARCH としてバックエンドに届くこと。"""
    source = EXTENSION_SRC.read_text(encoding="utf-8")
    assert 'config().get<boolean>("webSearch", true)' in source
    assert 'env.QQQ_NO_SEARCH = "1"' in source
    # 論理の向き: 設定が false のときに限って検索を止める
    assert re.search(
        r'if\s*\(\s*!config\(\)\.get<boolean>\("webSearch",\s*true\)\s*\)\s*\{\s*'
        r'env\.QQQ_NO_SEARCH\s*=\s*"1";',
        source,
    ), "webSearch が false のときだけ QQQ_NO_SEARCH を渡す形になっていない"
