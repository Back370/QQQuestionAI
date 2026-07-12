"""FastAPI ローカルサーバ（architecture.md §3）。

- git pre-commit フック: POST /quiz/start → GET /quiz/{sid}/status をポーリング
- VSCode 拡張: GET /quiz/pending をポーリングして新規セッションを拾い、
  Webview から answer / hint / giveup / abort を叩く
- 127.0.0.1 のみで待ち受ける。模範解答は問題が終わるまでレスポンスに含めない
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from . import diff_analyzer
from .knowledge_base import (
    KnowledgeBase,
    KnowledgeBaseBuilder,
    create_knowledge_base,
    create_search_provider,
)
from .learner_model import HistoryStore, load_learner_state
from .llm import StructuredLLM, create_llm
from .models import DiffContext
from .session import QuizSession

DEFAULT_PORT = 8756


@dataclass
class AppDeps:
    llm: StructuredLLM
    kb: KnowledgeBase
    data_dir: Path
    diff_provider: Callable[[str], DiffContext]
    kb_builder: KnowledgeBaseBuilder | None = None
    sessions: dict[str, QuizSession] = field(default_factory=dict)
    claimed: set[str] = field(default_factory=set)

    @property
    def history_path(self) -> Path:
        return self.data_dir / "history.jsonl"


def default_deps() -> AppDeps:
    data_dir = Path(os.environ.get("QQQ_DATA_DIR", "data"))
    data_dir.mkdir(parents=True, exist_ok=True)
    kb = create_knowledge_base(str(data_dir))
    return AppDeps(
        llm=create_llm(),
        kb=kb,
        data_dir=data_dir,
        diff_provider=lambda repo: diff_analyzer.analyze_staged(repo),
        kb_builder=KnowledgeBaseBuilder(
            kb, create_search_provider(), data_dir / "kb_cache.json"
        ),
    )


class StartRequest(BaseModel):
    repo_path: str = "."


class AnswerRequest(BaseModel):
    answer: str


def _session_or_404(deps: AppDeps, session_id: str) -> QuizSession:
    session = deps.sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    return session


def _answer_payload(result) -> dict:
    payload: dict = {
        "judgement": result.judgement.model_dump(),
        "question_done": result.question_done,
    }
    if result.question_done:
        payload["model_answer"] = result.model_answer
        payload["explanation"] = (
            result.explanation.model_dump() if result.explanation else None
        )
    return payload


def create_app(deps: AppDeps | None = None) -> FastAPI:
    app = FastAPI(title="QQQuestionAI backend")
    if deps is None:
        deps = default_deps()
    app.state.deps = deps

    @app.get("/health")
    def health():
        return {"status": "ok", "kb_chunks": deps.kb.count()}

    @app.post("/quiz/start")
    def start(request: StartRequest):
        diff_ctx = deps.diff_provider(request.repo_path)
        if not diff_ctx.diff_text.strip():
            raise HTTPException(status_code=400, detail="ステージ済みの差分がありません")
        if deps.kb_builder is not None:
            deps.kb_builder.build_for_topics(diff_ctx.topics)
        learner_state = load_learner_state(deps.history_path)
        session = QuizSession(
            llm=deps.llm,
            kb=deps.kb,
            diff_ctx=diff_ctx,
            learner_state=learner_state,
            history_store=HistoryStore(deps.history_path),
        )
        deps.sessions[session.id] = session
        return {
            "session_id": session.id,
            "topics": diff_ctx.topics,
            "files": diff_ctx.files,
            "kb_chunks": deps.kb.count(),
            "weak_topics": learner_state.weak_topics(),
            "total": session.total,
        }

    @app.get("/quiz/pending")
    def pending():
        """UI が未表示のセッション一覧。返したものは claimed 扱いにする。"""
        found = []
        for session in deps.sessions.values():
            if session.status == "in_progress" and session.id not in deps.claimed:
                deps.claimed.add(session.id)
                found.append(
                    {
                        "session_id": session.id,
                        "topics": session.diff_ctx.topics,
                        "files": session.diff_ctx.files,
                        "total": session.total,
                    }
                )
        return {"sessions": found}

    @app.get("/quiz/{session_id}/status")
    def status(session_id: str):
        session = _session_or_404(deps, session_id)
        return {"status": session.status}

    @app.get("/quiz/{session_id}/question")
    def question(session_id: str):
        session = _session_or_404(deps, session_id)
        return {"question": session.current_public(), "status": session.status}

    @app.post("/quiz/{session_id}/answer")
    def answer(session_id: str, request: AnswerRequest):
        session = _session_or_404(deps, session_id)
        if session.finished:
            raise HTTPException(status_code=409, detail="全問終了しています")
        result = session.submit_answer(request.answer)
        payload = _answer_payload(result)
        payload["next_question"] = session.current_public()
        payload["status"] = session.status
        return payload

    @app.post("/quiz/{session_id}/hint")
    def hint(session_id: str):
        session = _session_or_404(deps, session_id)
        if session.finished:
            raise HTTPException(status_code=409, detail="全問終了しています")
        return {"hint": session.request_hint().model_dump()}

    @app.post("/quiz/{session_id}/giveup")
    def giveup(session_id: str):
        session = _session_or_404(deps, session_id)
        if session.finished:
            raise HTTPException(status_code=409, detail="全問終了しています")
        result = session.give_up()
        payload = _answer_payload(result)
        payload["next_question"] = session.current_public()
        payload["status"] = session.status
        return payload

    @app.post("/quiz/{session_id}/abort")
    def abort(session_id: str):
        session = _session_or_404(deps, session_id)
        session.abort()
        return {"status": session.status}

    @app.get("/quiz/{session_id}/report")
    def report(session_id: str):
        session = _session_or_404(deps, session_id)
        session_report = session.report()
        return {
            "report": {
                "attempted": session_report.attempted,
                "first_correct_rate": session_report.first_correct_rate,
                "final_correct_rate": session_report.final_correct_rate,
                "hints_shown": session_report.hints_shown,
                "leak_rate": session_report.leak_rate,
                "hint_effective_rate": session_report.hint_effective_rate,
                "groundedness": session_report.groundedness,
                "weak_topic_notes": session_report.weak_topic_notes,
                "completed": session_report.completed,
            },
            "rendered": session_report.render(),
        }

    return app


def main() -> None:
    import uvicorn

    from .envfile import load_env_file

    load_env_file()  # backend/.env から GOOGLE_API_KEY 等を読み込む（任意）
    port = int(os.environ.get("QQQ_PORT", DEFAULT_PORT))
    uvicorn.run(create_app(), host="127.0.0.1", port=port)


if __name__ == "__main__":
    main()
