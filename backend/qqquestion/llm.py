"""LLM 抽象化層。

役割ごとに Pydantic スキーマを指定した structured output で呼び出す
（architecture.md §5.1）。Gemini 実装は遅延 import にして、
API キーや langchain が無い環境（テスト・オフライン）でも
FakeLLM で全ロジックが動くようにする。
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Iterator, Protocol, TypeVar

from pydantic import BaseModel

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# ストリーミング生成のイベント。
#   ("partial", dict)      — その時点までの部分パース結果（文字列は途中まで）
#   ("final", BaseModel)   — 検証済みの最終結果。必ず最後に1回だけ流れる
StreamEvent = tuple[str, object]

# 既定モデル。Google は退役したモデルを「新規プロジェクトからは 404」にして段階的に
# 閉じるため、既定値が古いと"新しくAPIキーを取った人だけ"が壊れる（既存キーでは再現
# しない）。過去 2.0→2.5→3.5 と踏んでいるので、404 は _classify_llm_error が
# QQQ_MODEL での回避を案内できるようにしてある。
DEFAULT_MODEL = "gemini-3.5-flash"

# 1回のLLM呼び出しの応答待ち上限（秒）。これを超えると打ち切って例外にする。
# タイムアウトが無いと、API無応答・レート制限のリトライ待ちで prepare_first() が
# 返らず、UI が「問題を生成中…」のまま固まる（ハングの主因）。QQQ_LLM_TIMEOUT で調整可。
DEFAULT_TIMEOUT = 45.0
# langchain-google-genai の既定リトライは6回（指数バックオフ）で、無応答時に
# 待ち時間が数分に膨れる。ハングを避けるため回数を絞る。
DEFAULT_MAX_RETRIES = 2

_NO_KEY_MESSAGE = (
    "GOOGLE_API_KEY が設定されていません。"
    "backend/.env に GOOGLE_API_KEY=... を書くか（env.example 参照）、"
    "環境変数で渡してください。オフラインで試すには QQQ_FAKE_LLM=1 です。"
)


class LLMUnavailableError(RuntimeError):
    """LLM(API) が利用できないときのエラー。

    message はそのまま UI に出せる日本語にする（fail-open 経路が session.error
    に載せ、拡張が「生成に失敗したためスキップ」として表示する）。
    """


def _llm_timeout() -> float:
    try:
        return float(os.environ.get("QQQ_LLM_TIMEOUT", DEFAULT_TIMEOUT))
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT


def _current_model_name() -> str:
    return os.environ.get("QQQ_MODEL", DEFAULT_MODEL)


def _is_model_missing_error(error: Exception) -> bool:
    """モデルが存在しない/提供終了で使えない失敗か（404 NOT_FOUND）。"""
    text = str(error).lower()
    return "404" in text or "not_found" in text or "not found" in text


def _is_unavailable_error(error: Exception) -> bool:
    """API そのものが使えない失敗（タイムアウト・レート制限・認証・モデル不在）か。

    これらは速度優先の generate_fast から通常経路へフォールバックしても直らない
    ので、二重待ちを避けて即座に伝える判断に使う。
    """
    if isinstance(error, TimeoutError):
        return True
    if _is_model_missing_error(error):
        return True
    text = str(error).lower()
    keywords = (
        "timeout", "deadline", "429", "quota", "rate limit", "exhausted",
        "api key", "api_key", "unauthenticated", "permission", "401", "403",
    )
    return any(word in text for word in keywords)


def _fast_thinking_kwargs(model_name: str) -> dict[str, object]:
    """速度優先時に thinking を最小化する ChatGoogleGenerativeAI の引数。

    Gemini 3 以降は thinking_budget を受け付けず、thinking_level（最小でも
    "minimal"、完全な無効化は不可）で制御する。世代を見ずに thinking_budget=0 を
    送ると API に弾かれ、失敗する往復を1回挟んでから通常生成へ落ちるため、
    速度優先のはずが逆に遅くなる。
    """
    match = re.match(r"gemini-(\d+)", model_name)
    if match and int(match.group(1)) >= 3:
        return {"thinking_level": "minimal"}
    return {"thinking_budget": 0}


def _classify_llm_error(error: Exception) -> str:
    """LLM 呼び出しの失敗を、利用者が次の一手を打てる日本語メッセージに変換する。"""
    text = str(error).lower()
    if isinstance(error, TimeoutError) or "timeout" in text or "deadline" in text:
        return (
            "AIサービスが時間内に応答しませんでした（タイムアウト）。"
            "ネットワークやAPIの状態を確認し、再度お試しください。"
        )
    if "429" in text or "quota" in text or "rate limit" in text or "exhausted" in text:
        return (
            "AIサービスの利用上限（レート制限・クォータ）に達しました。"
            "しばらく待つか、別のAPIキーで再試行してください。"
        )
    if (
        "api key" in text
        or "api_key" in text
        or "unauthenticated" in text
        or "permission" in text
        or "401" in text
        or "403" in text
    ):
        return "APIキーが無効か権限がありません。GOOGLE_API_KEY を確認してください。"
    if _is_model_missing_error(error):
        return (
            f"AIモデル「{_current_model_name()}」が使えません（提供終了か名前の誤り）。"
            "環境変数 QQQ_MODEL に現行のモデル名を設定して再試行してください。"
            "使えるモデルは https://ai.google.dev/gemini-api/docs/models で確認できます。"
        )
    return f"AIサービスの呼び出しに失敗しました: {error}"


class StructuredLLM(Protocol):
    """全役割共通の LLM インタフェース。"""

    def generate(
        self, schema: type[T], system: str, user: str, temperature: float = 0.0
    ) -> T: ...


class GeminiLLM:
    """LangChain 経由の Gemini 実装（architecture.md §2）。"""

    def __init__(self, model: str | None = None):
        self._model_name = model or _current_model_name()
        # APIキーの検査は呼び出し時に遅延させる。起動時に例外を投げると
        # サーバが立ち上がらず、ユーザーに何も表示できないまま静かにスキップに
        # なるため。呼び出し時に LLMUnavailableError を投げれば、fail-open 経路が
        # UI に「APIキー未設定」を出せる。

    def _require_key(self) -> None:
        if not os.environ.get("GOOGLE_API_KEY"):
            raise LLMUnavailableError(_NO_KEY_MESSAGE)

    def _chat(self, temperature: float, **extra):
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(
            model=self._model_name,
            temperature=temperature,
            timeout=_llm_timeout(),
            max_retries=DEFAULT_MAX_RETRIES,
            **extra,
        )

    def generate(
        self, schema: type[T], system: str, user: str, temperature: float = 0.0
    ) -> T:
        self._require_key()
        from langchain_core.messages import HumanMessage, SystemMessage

        chat = self._chat(temperature).with_structured_output(schema)
        try:
            result = chat.invoke(
                [SystemMessage(content=system), HumanMessage(content=user)]
            )
        except Exception as error:
            # UI には分類済みメッセージしか出ないため、生のエラーをログに残す
            logger.warning(
                "LLM呼び出しに失敗: model=%s schema=%s error=%r",
                self._model_name,
                schema.__name__,
                error,
            )
            raise LLMUnavailableError(_classify_llm_error(error)) from error
        if isinstance(result, dict):
            result = schema.model_validate(result)
        return result

    def generate_fast(
        self, schema: type[T], system: str, user: str, temperature: float = 0.0
    ) -> T:
        """thinking を最小化した速度優先の生成（実測で約6.9秒→2.9秒）。

        第1問の先行生成など、体感待ち時間が最重要の呼び出しで使う。
        パラメータはモデル世代で異なる（_fast_thinking_kwargs 参照）。非対応の
        モデル・ライブラリでは通常の generate にフォールバックする。ただし API
        自体が使えない（キー無し・タイムアウト・レート制限・モデル提供終了）場合は
        フォールバックしても同じく失敗するため、そのまま LLMUnavailableError を
        投げて二重待ちを避ける。
        """
        self._require_key()
        from langchain_core.messages import HumanMessage, SystemMessage

        try:
            chat = self._chat(
                temperature, **_fast_thinking_kwargs(self._model_name)
            ).with_structured_output(schema)
            result = chat.invoke(
                [SystemMessage(content=system), HumanMessage(content=user)]
            )
            if isinstance(result, dict):
                result = schema.model_validate(result)
            return result
        except LLMUnavailableError:
            raise
        except Exception as error:
            # API の利用不能はフォールバックしても直らないので即座に伝える。
            # それ以外（thinking パラメータ非対応など）は通常経路で作り直す。
            if _is_unavailable_error(error):
                logger.warning(
                    "LLM呼び出し(fast)に失敗: model=%s schema=%s error=%r",
                    self._model_name,
                    schema.__name__,
                    error,
                )
                raise LLMUnavailableError(_classify_llm_error(error)) from error
            logger.info(
                "fast生成に失敗したため通常生成へフォールバック: model=%s error=%r",
                self._model_name,
                error,
            )
            return self.generate(schema, system, user, temperature=temperature)

    def generate_stream(
        self, schema: type[T], system: str, user: str, temperature: float = 0.0
    ) -> Iterator[StreamEvent]:
        """JSONモードでストリーミングし、部分パース結果を逐次 yield する。

        ストリーム途中の失敗・最終検証エラー時は非ストリームの generate() に
        フォールバックする（呼び出し側は snapshot 置き換えで表示する前提）。
        API 自体が使えない場合は generate() 側で LLMUnavailableError になる。
        """
        self._require_key()
        from langchain_core.messages import HumanMessage, SystemMessage
        from langchain_core.utils.json import parse_partial_json

        schema_note = (
            "\n\n出力は次の JSON Schema に従う JSON オブジェクトのみとし、"
            "コードフェンスや前置きを付けないこと:\n"
            + json.dumps(schema.model_json_schema(), ensure_ascii=False)
        )
        chat = self._chat(temperature, response_mime_type="application/json")
        buffer = ""
        last_partial: dict | None = None
        final: T | None = None
        try:
            for chunk in chat.stream(
                [SystemMessage(content=system + schema_note), HumanMessage(content=user)]
            ):
                buffer += _chunk_text(chunk.content)
                try:
                    partial = parse_partial_json(_strip_fences(buffer))
                except Exception:
                    continue
                if isinstance(partial, dict) and partial != last_partial:
                    last_partial = partial
                    yield ("partial", partial)
            final = schema.model_validate(json.loads(_strip_fences(buffer)))
        except Exception as error:
            logger.info(
                "ストリーミング生成に失敗したため非ストリーム生成へフォールバック: "
                "model=%s schema=%s error=%r",
                self._model_name,
                schema.__name__,
                error,
            )
            final = None
        if final is None:
            # generate() はタイムアウト等を LLMUnavailableError に変換して投げる
            final = self.generate(schema, system, user, temperature=temperature)
            yield ("partial", final.model_dump())
        yield ("final", final)


class FakeLLM:
    """テスト・デモ用の決定的 LLM。

    スキーマ型ごとに応答キューを持ち、enqueue した順に返す。
    キューが空のときは default_factory があればそれを使う。
    """

    def __init__(self):
        self._queues: dict[type, list[BaseModel]] = {}
        self._defaults: dict[type, object] = {}
        self.calls: list[dict] = []

    def enqueue(self, response: BaseModel) -> None:
        self._queues.setdefault(type(response), []).append(response)

    def set_default(self, schema: type[T], factory) -> None:
        """キューが尽きたとき factory(system, user) で応答を作る。"""
        self._defaults[schema] = factory

    def generate(
        self, schema: type[T], system: str, user: str, temperature: float = 0.0
    ) -> T:
        self.calls.append(
            {"schema": schema.__name__, "system": system, "user": user,
             "temperature": temperature}
        )
        queue = self._queues.get(schema)
        if queue:
            return queue.pop(0)  # type: ignore[return-value]
        factory = self._defaults.get(schema)
        if factory is not None:
            return factory(system, user)  # type: ignore[return-value]
        raise AssertionError(f"FakeLLM: {schema.__name__} の応答が用意されていません")

    def generate_stream(
        self, schema: type[T], system: str, user: str, temperature: float = 0.0
    ) -> Iterator[StreamEvent]:
        """確定済み応答をフィールド順に数文字ずつ育てて疑似ストリームする。"""
        final = self.generate(schema, system, user, temperature=temperature)
        partial: dict = {}
        for key, value in final.model_dump().items():
            if isinstance(value, str) and value:
                for end in range(4, len(value) + 4, 4):
                    partial[key] = value[:end]
                    yield ("partial", dict(partial))
            else:
                partial[key] = value
                yield ("partial", dict(partial))
        yield ("final", final)


def _chunk_text(content: object) -> str:
    """LangChain のチャンク content からテキストを取り出す（str / parts 両対応）。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
        return "".join(parts)
    return ""


def _strip_fences(text: str) -> str:
    """モデルが付けがちな ```json フェンスを剥がす（ストリーム途中でも安全）。"""
    stripped = text.lstrip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[1] if "\n" in stripped else ""
        end = stripped.rfind("```")
        if end != -1:
            stripped = stripped[:end]
    return stripped.strip()


def fast_generate(
    llm: StructuredLLM, schema: type[T], system: str, user: str, temperature: float = 0.0
) -> T:
    """速度優先の生成。generate_fast を持つ実装（Gemini: thinking 無効）を優先し、
    無ければ通常の generate にフォールバックする。"""
    method = getattr(llm, "generate_fast", None)
    if method is not None:
        return method(schema, system, user, temperature=temperature)
    return llm.generate(schema, system, user, temperature=temperature)


def stream_generate(
    llm: StructuredLLM, schema: type[T], system: str, user: str, temperature: float = 0.0
) -> Iterator[StreamEvent]:
    """generate_stream を持たない実装でも動く共通入口。

    持たない場合はワンショット生成の結果を partial → final の2イベントで流す。
    """
    method = getattr(llm, "generate_stream", None)
    if method is None:
        final = llm.generate(schema, system, user, temperature=temperature)
        yield ("partial", final.model_dump())
        yield ("final", final)
        return
    yield from method(schema, system, user, temperature=temperature)


def create_llm() -> StructuredLLM:
    """環境変数から適切な LLM を返すファクトリ。

    QQQ_FAKE_LLM=1 のときはデモ用 FakeLLM（RNN 教材の缶詰問題入り）。
    """
    if os.environ.get("QQQ_FAKE_LLM") == "1":
        from .demo import build_demo_llm

        return build_demo_llm()
    return GeminiLLM()
