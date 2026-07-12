# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

安全ルールを含む全AIエージェント共通の指示は以下からインポートされる。**ルールの本体はAGENTS.md側にあり、変更もそちらで行う**——このファイルにはClaude Code固有の事項だけを書くこと（二重管理による食い違いを防ぐため）。

@AGENTS.md

## Project

QQQuestionAI — コミット前・コード理解度チェック学習支援AI。`git commit -q` をトリガーに、ステージ済み差分から記述式5問（前提知識2問＋実装説明3問）を出題し、答えを教えずヒントだけで理解を確認する。設計は [docs/direction.md](docs/direction.md)（要件）と [docs/architecture.md](docs/architecture.md)（実装方針）、想定対話は [docs/dialogue_examples/](docs/dialogue_examples/) を参照。

## Build/Run/Test Commands

```bash
# バックエンド（Python 3.11+。venv は backend/.venv）
cd backend
python3 -m venv .venv && .venv/bin/pip install -e . fastapi uvicorn pytest httpx
.venv/bin/pip install langchain-core langchain-google-genai chromadb ddgs  # LLM/知識ベース用

.venv/bin/python -m pytest                    # テスト（LLM/APIキー不要、FakeLLMで動く）
QQQ_FAKE_LLM=1 .venv/bin/python -m qqquestion.cli --demo   # APIキー不要のCLIデモ
GOOGLE_API_KEY=... .venv/bin/python -m qqquestion.server   # 本番サーバ (127.0.0.1:8756)
QQQ_FAKE_LLM=1 .venv/bin/python -m qqquestion.evaluator data/eval_set.json  # 判定精度のオフライン評価

# VSCode 拡張
cd extension && npm install && npm run compile   # F5 (拡張開発ホスト) で起動
npx @vscode/vsce package --allow-missing-repository  # 配布用 .vsix を作成

# git フック（対象リポジトリで実行）
scripts/install_quiz_hook.sh   # pre-commit フック + `-q` 検知シェル関数の案内
```

主要な環境変数: `GOOGLE_API_KEY`（Gemini）、`QQQ_FAKE_LLM=1`（APIキー不要のデモ）、`QQQ_PORT`（既定 8756）、`QQQ_DATA_DIR`（履歴/知識ベース保存先、既定 `data/`）、`QQQ_NO_SEARCH=1`（Web検索無効化）、`TAVILY_API_KEY`（検索。無ければ ddgs）。APIキーは `backend/env.example` を `backend/.env` にコピーして設定できる（サーバ/CLI起動時に自動読み込み。既存の環境変数が優先）。AGENTS.mdの安全ルール2のとおり、AIエージェントは `.env` を読まない・コミットしない。

## Architecture Overview

- `backend/qqquestion/` — Python バックエンド。`session.py`（出題→判定→ヒント→解説の状態機械）を中心に、`question_gen` / `judge` / `hint_gen` / `explainer`（LLM呼び出し、Pydantic structured output）、`diff_analyzer`（差分→トピック抽出、ルールベース）、`knowledge_base`（Web検索→ChromaDB、RAG）、`learner_model`（苦手傾向の記録・反映）、`evaluator`（評価レポート）、`server.py`（FastAPI）、`cli.py`（ターミナル版）。
- `extension/` — VSCode 拡張（TypeScript）。バックエンドを自動起動し `/quiz/pending` をポーリング、Webview クイズパネルを表示。
- `scripts/hooks/qqquestion-pre-commit` — `QQQ_QUIZ=1` のときだけ発動する pre-commit フック。クイズ完走で exit 0、パネルを閉じる（中断）と exit 1 でコミット中止。バックエンド未起動なら素通し。

## Key Conventions

- LLM 依存は `llm.StructuredLLM` プロトコルに集約し、テストは `FakeLLM` で書く（実APIを叩くテストを追加しない）
- 模範解答・採点基準は出題時に確定し、`Question.public_view()` 以外で UI に渡さない（答え漏洩防止）
- 重い依存（langchain / chromadb / ddgs）は遅延 import。無い環境でもコアループが動くフォールバックを保つ
- テスト実行は `cd backend && .venv/bin/python -m pytest`。機能追加時は対応するテストを `backend/tests/` に追加する

## Claude Code固有の補足（このセクションはテンプレートを埋めた後も残すこと）

- AGENTS.mdの安全ルール1（破壊的コマンド）は、Claude Codeでは `.claude/settings.json` の `permissions.deny` と `.claude/hooks/deny_dangerous_bash.py`（PreToolUse hook）により**強制**される。hookの検出パターンを変更したら `python3 .claude/hooks/test_deny_dangerous_bash.py` で回帰テストを必ず実行する。
- AGENTS.mdのランブック（safe-rollback / go-live-checklist / project-health-check）は、Claude Codeではスキルとして自動発動する。「公開して」と言われても go-live-checklist の監査を通さずにデプロイへ進まない。「壊れた」と言われたら safe-rollback に従い、`git reset --hard` や force push で回復しない。
- テンプレートリポジトリでは `.github/workflows/verify-template.yml` が安全網の整合性（`scripts/verify_safety_net.py`）を毎push検査する。