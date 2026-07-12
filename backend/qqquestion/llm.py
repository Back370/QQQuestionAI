"""LLM 抽象化層。

役割ごとに Pydantic スキーマを指定した structured output で呼び出す
（architecture.md §5.1）。Gemini 実装は遅延 import にして、
API キーや langchain が無い環境（テスト・オフライン）でも
FakeLLM で全ロジックが動くようにする。
"""

from __future__ import annotations

import os
from typing import Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

DEFAULT_MODEL = "gemini-2.0-flash"


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


def create_llm() -> StructuredLLM:
    """環境変数から適切な LLM を返すファクトリ。

    QQQ_FAKE_LLM=1 のときはデモ用 FakeLLM（RNN 教材の缶詰問題入り）。
    """
    if os.environ.get("QQQ_FAKE_LLM") == "1":
        from .demo import build_demo_llm

        return build_demo_llm()
    return GeminiLLM()
