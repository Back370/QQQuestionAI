"""LLM 抽象化層。

役割ごとに Pydantic スキーマを指定した structured output で呼び出す
（architecture.md §5.1）。Gemini 実装は遅延 import にして、
API キーや langchain が無い環境（テスト・オフライン）でも
FakeLLM で全ロジックが動くようにする。
"""

from __future__ import annotations

import json
import os
from typing import Iterator, Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

# ストリーミング生成のイベント。
#   ("partial", dict)      — その時点までの部分パース結果（文字列は途中まで）
#   ("final", BaseModel)   — 検証済みの最終結果。必ず最後に1回だけ流れる
StreamEvent = tuple[str, object]

# gemini-2.0-flash は無料枠の割り当てが終了している(429 limit:0)ため 2.5 を既定にする
DEFAULT_MODEL = "gemini-2.5-flash"


class StructuredLLM(Protocol):
    """全役割共通の LLM インタフェース。"""

    def generate(
        self, schema: type[T], system: str, user: str, temperature: float = 0.0
    ) -> T: ...


class GeminiLLM:
    """LangChain 経由の Gemini 実装（architecture.md §2）。"""

    def __init__(self, model: str | None = None):
        self._model_name = model or os.environ.get("QQQ_MODEL", DEFAULT_MODEL)
        if not os.environ.get("GOOGLE_API_KEY"):
            raise RuntimeError(
                "GOOGLE_API_KEY が設定されていません。"
                "backend/.env に GOOGLE_API_KEY=... を書くか（env.example 参照）、"
                "環境変数で渡してください。オフラインで試すには QQQ_FAKE_LLM=1 です。"
            )

    def generate(
        self, schema: type[T], system: str, user: str, temperature: float = 0.0
    ) -> T:
        from langchain_core.messages import HumanMessage, SystemMessage
        from langchain_google_genai import ChatGoogleGenerativeAI

        chat = ChatGoogleGenerativeAI(
            model=self._model_name, temperature=temperature
        ).with_structured_output(schema)
        result = chat.invoke([SystemMessage(content=system), HumanMessage(content=user)])
        if isinstance(result, dict):
            result = schema.model_validate(result)
        return result

    def generate_stream(
        self, schema: type[T], system: str, user: str, temperature: float = 0.0
    ) -> Iterator[StreamEvent]:
        """JSONモードでストリーミングし、部分パース結果を逐次 yield する。

        ストリーム途中の失敗・最終検証エラー時は非ストリームの generate() に
        フォールバックする（呼び出し側は snapshot 置き換えで表示する前提）。
        """
        from langchain_core.messages import HumanMessage, SystemMessage
        from langchain_core.utils.json import parse_partial_json
        from langchain_google_genai import ChatGoogleGenerativeAI

        schema_note = (
            "\n\n出力は次の JSON Schema に従う JSON オブジェクトのみとし、"
            "コードフェンスや前置きを付けないこと:\n"
            + json.dumps(schema.model_json_schema(), ensure_ascii=False)
        )
        chat = ChatGoogleGenerativeAI(
            model=self._model_name,
            temperature=temperature,
            response_mime_type="application/json",
        )
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
        except Exception:
            final = None
        if final is None:
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
