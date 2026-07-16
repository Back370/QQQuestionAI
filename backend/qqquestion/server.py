"""FastAPI ローカルサーバ（architecture.md §3）。

- git pre-commit フック: POST /quiz/start → GET /quiz/{sid}/status をポーリング
- VSCode 拡張: GET /quiz/pending をポーリングしてフック起点のセッションを拾い、
  Webview から answer / hint / giveup / abort を叩く（フックは自前の UI を
  持たないため、この経路でしか出題できない）
- 127.0.0.1 のみで待ち受ける。模範解答は問題が終わるまでレスポンスに含めない
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse
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
from .models import DiffContext, QuizOrigin
from .session import QuizSession

DEFAULT_PORT = 8756

# セッションの repo_path がどのウィンドウのワークスペースにも一致しないまま
# この秒数を過ぎたら、取りこぼし防止のためどのウィンドウでも拾えるようにする
# （パス不一致・対象リポジトリがどのウィンドウでも開かれていない場合の保険）。
PENDING_GRACE_SECONDS = 5.0

# `python -m qqquestion.server` 起動では __name__ が __main__ になるため明示する
logger = logging.getLogger("qqquestion.server")


def _spawn_daemon_thread(task: Callable[[], None]) -> None:
    threading.Thread(target=task, daemon=True).start()


@dataclass
class AppDeps:
    llm: StructuredLLM
    kb: KnowledgeBase
    data_dir: Path
    diff_provider: Callable[[str], DiffContext]
    kb_builder: KnowledgeBaseBuilder | None = None
    sessions: dict[str, QuizSession] = field(default_factory=dict)
    claimed: set[str] = field(default_factory=set)
    # ワークスペース不一致のまま未 claim のセッションを最初に見た時刻（保険用）
    pending_since: dict[str, float] = field(default_factory=dict)
    # 残り問題の生成・知識ベース構築を実行する場所（テストでは同期実行に差し替える）
    run_in_background: Callable[[Callable[[], None]], None] = _spawn_daemon_thread

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
    # 既定を "hook" にするのは後方互換のため。origin を送らない旧クライアントは
    # 旧フックの可能性があり、パネルが開かないとコミットが進まず固まる。誤って
    # パネルが開くほうが、待ち続けて固まるより安全（従来の挙動と同じ）。
    origin: QuizOrigin = "hook"


class AnswerRequest(BaseModel):
    answer: str


def _normalize_path(p: str) -> str:
    """比較用にパスを正規化する。symlink（macOS の /tmp→/private/tmp 等）も解決。

    フックは `$(pwd)`、拡張は VSCode の fsPath を送ってくるため、実体パスに
    揃えないと同じディレクトリでも文字列一致しないことがある。
    """
    return os.path.realpath(p)


def _repo_matches_workspaces(repo_path: str | None, workspaces: list[str]) -> bool:
    """セッションの repo_path が、いずれかのワークスペースに属するか。

    ワークスペース＝リポジトリのルートが基本だが、サブディレクトリを開いて
    いる／サブディレクトリで commit した場合もあるので、どちらかがもう一方を
    包含していれば同一ウィンドウとみなす。
    """
    if not repo_path:
        return False
    repo = _normalize_path(repo_path)
    for ws in workspaces:
        if repo == ws or repo.startswith(ws + os.sep) or ws.startswith(repo + os.sep):
            return True
    return False


def _session_or_404(deps: AppDeps, session_id: str) -> QuizSession:
    session = deps.sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    return session


def _sse(event: str, data: dict) -> str:
    """SSE の1イベント。クライアントは data 行の JSON だけ見ればよい。"""
    return "data: " + json.dumps({"event": event, **data}, ensure_ascii=False) + "\n\n"


def _public_judgement(judgement, question_done: bool) -> dict:
    """UI へ返す判定。問題が終わるまでは採点内部情報を伏せる。

    matched_points / missing_points は accepted_points（＝正解の骨子）を
    そのまま含むため、問題完了前は空にする。incorrect の reason も欠けた
    要点＝答えの手がかりを含みうるので伏せる（ストリーミング経路が途中
    経過を流さないのと同じ方針）。partial の reason は「あと何が足りないか」
    の学習フィードバックとして残す。問題完了後は模範解答が開示されるため
    そのまま返す。
    """
    data = judgement.model_dump()
    if question_done:
        return data
    data["matched_points"] = []
    data["missing_points"] = []
    if data.get("verdict") == "incorrect":
        data["reason"] = ""
    return data


def _sse_response(session: QuizSession, events: Iterator[tuple[str, object]]):
    """セッションのストリームイベントを SSE に変換する。

    最後の result イベントは非ストリーム版 /answer と同じフィールドを持つ。
    """

    def generate() -> Iterator[str]:
        for name, payload in events:
            if name == "result":
                data = _answer_payload(payload)
                data["next_question"] = session.current_public()
                data["status"] = session.status
                yield _sse("result", data)
            elif name == "judgement":
                done = payload["question_done"]  # type: ignore[index]
                data = {
                    "judgement": _public_judgement(payload["judgement"], done),  # type: ignore[index]
                    "question_done": done,
                }
                if done:
                    data["model_answer"] = payload["model_answer"]  # type: ignore[index]
                yield _sse("judgement", data)
            else:  # judgement_partial / explanation_partial
                yield _sse(name, payload)  # type: ignore[arg-type]

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _answer_payload(result) -> dict:
    payload: dict = {
        "judgement": _public_judgement(result.judgement, result.question_done),
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
        logger.info("クイズ開始要求: repo=%s origin=%s", request.repo_path, request.origin)
        diff_ctx = deps.diff_provider(request.repo_path)
        if not diff_ctx.diff_text.strip():
            logger.warning("ステージ済みの差分が無いため 400 を返します: repo=%s", request.repo_path)
            raise HTTPException(status_code=400, detail="ステージ済みの差分がありません")
        learner_state = load_learner_state(deps.history_path)
        session = QuizSession(
            llm=deps.llm,
            kb=deps.kb,
            diff_ctx=diff_ctx,
            learner_state=learner_state,
            history_store=HistoryStore(deps.history_path),
            defer_questions=True,
            repo_path=os.path.abspath(request.repo_path),
            origin=request.origin,
        )
        # 生成前にセッションを公開する: 拡張の /quiz/pending ポーリングが即座に
        # 拾ってパネルを開き、「生成中」を表示できる
        deps.sessions[session.id] = session
        # 第1問だけ同期生成して即出題する。残り4問と知識ベース構築（Web検索）は
        # バックグラウンドに回し、第1問の解答中に進める
        session.prepare_first(fail_open=True)
        if session.error:
            logger.error(
                "第1問の生成に失敗（fail-open でコミットは通す）: session=%s error=%s",
                session.id,
                session.error,
            )
        else:
            logger.info(
                "セッション開始: session=%s files=%s topics=%s",
                session.id,
                diff_ctx.files,
                diff_ctx.topics,
            )

        def prepare_in_background() -> None:
            session.prepare_rest(fail_open=True)
            if session.error:
                logger.warning(
                    "残り問題の生成に失敗（確定済みの問題だけで続行）: session=%s error=%s",
                    session.id,
                    session.error,
                )
            if deps.kb_builder is not None and session.status == "in_progress":
                deps.kb_builder.build_for_topics(diff_ctx.topics)

        if session.status == "in_progress":
            deps.run_in_background(prepare_in_background)
        return {
            "session_id": session.id,
            "topics": diff_ctx.topics,
            "files": diff_ctx.files,
            "kb_chunks": deps.kb.count(),
            "weak_topics": learner_state.weak_topics(),
            "total": session.total,
            "error": session.error,
        }

    @app.get("/quiz/pending")
    def pending(workspace: list[str] = Query(default=[])):
        """パネルを開いてほしいセッション一覧。返したものは claimed 扱いにする。

        返すのは origin="hook" のセッションだけ。フックは curl のシェル
        スクリプトで自前の UI を持たず、拡張にパネルを開いてもらう以外に
        出題する手段がないため、このポーリングがフックから VSCode への唯一の
        通知経路になっている。一方 cli（端末の quiz）と ui（拡張のコマンド）は
        既に自分の UI で出題しているので、ここで返すと二重表示になる。

        複数の VSCode ウィンドウが同じバックエンドを共有するため、コミットが
        走ったリポジトリ（session.repo_path）を、ポーリング元ウィンドウの
        ワークスペース（workspace クエリ）と突き合わせ、担当ウィンドウにだけ
        返す。これで別ウィンドウにクイズパネルが開く問題を防ぐ。
        workspace 未指定（旧拡張）のときは従来どおり即 claim する。
        """
        found: list[dict] = []
        workspaces = [_normalize_path(w) for w in workspace]
        now = time.monotonic()

        def claim(session: QuizSession) -> None:
            deps.claimed.add(session.id)
            deps.pending_since.pop(session.id, None)
            found.append(
                {
                    "session_id": session.id,
                    "topics": session.diff_ctx.topics,
                    "files": session.diff_ctx.files,
                    "total": session.total,
                }
            )

        for session in deps.sessions.values():
            if session.status != "in_progress" or session.id in deps.claimed:
                continue
            if session.origin != "hook":
                continue  # cli / ui は自前の UI で出題済み。パネルを開かない
            if not workspaces or _repo_matches_workspaces(session.repo_path, workspaces):
                claim(session)
                continue
            # このウィンドウの担当ではない。担当ウィンドウが猶予内に拾えなければ
            # （パス不一致・対象リポジトリが未オープン等）取りこぼし防止で誰でも拾う
            first_seen = deps.pending_since.setdefault(session.id, now)
            if now - first_seen >= PENDING_GRACE_SECONDS:
                claim(session)
        return {"sessions": found}

    @app.get("/quiz/{session_id}/status")
    def status(session_id: str):
        session = _session_or_404(deps, session_id)
        return {"status": session.status}

    @app.get("/quiz/{session_id}/question")
    def question(session_id: str):
        session = _session_or_404(deps, session_id)
        return {
            "question": session.current_public(),
            "status": session.status,
            "preparing": session.preparing,
            "error": session.error,
        }

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

    @app.post("/quiz/{session_id}/answer/stream")
    def answer_stream(session_id: str, request: AnswerRequest):
        """半二重ストリーミング版 answer。判定理由・解説を出た側から流す。"""
        session = _session_or_404(deps, session_id)
        if session.finished:
            raise HTTPException(status_code=409, detail="全問終了しています")
        return _sse_response(session, session.submit_answer_stream(request.answer))

    @app.post("/quiz/{session_id}/giveup/stream")
    def giveup_stream(session_id: str):
        """半二重ストリーミング版 giveup。解説を出た側から流す。"""
        session = _session_or_404(deps, session_id)
        if session.finished:
            raise HTTPException(status_code=409, detail="全問終了しています")
        return _sse_response(session, session.give_up_stream())

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
        logger.info("セッション中断: session=%s status=%s", session_id, session.status)
        return {"status": session.status}

    @app.get("/quiz/{session_id}/report")
    def report(session_id: str):
        session = _session_or_404(deps, session_id)
        session_report = session.report()
        return {
            "status": session.status,
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
    from .logsetup import setup_file_logging

    load_env_file()  # backend/.env から GOOGLE_API_KEY 等を読み込む（任意）
    log_path = setup_file_logging(Path(os.environ.get("QQQ_DATA_DIR", "data")))
    logger.info("バックエンド起動: ログファイル=%s", log_path)
    port = int(os.environ.get("QQQ_PORT", DEFAULT_PORT))
    # log_config=None: uvicorn 既定設定で propagate が切れてファイルに
    # アクセスログ・例外ログが届かなくなるのを防ぐ（logsetup.py 参照）
    uvicorn.run(create_app(), host="127.0.0.1", port=port, log_config=None)


if __name__ == "__main__":
    main()
