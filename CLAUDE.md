# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

安全ルールを含む全AIエージェント共通の指示は以下からインポートされる。**ルールの本体はAGENTS.md側にあり、変更もそちらで行う**——このファイルにはClaude Code固有の事項だけを書くこと（二重管理による食い違いを防ぐため）。

@AGENTS.md

## Project

This repository is a template for AI-powered applications. It is currently empty — populate it with your project files and update this file accordingly.

## Getting Started

Add your project files, then update this CLAUDE.md with:

- **Build/run/test commands** — how to install dependencies, start the app, run tests, and lint
- **Architecture overview** — the high-level structure and how components interact
- **Key conventions** — any naming, formatting, or workflow rules specific to this project

## Claude Code固有の補足（このセクションはテンプレートを埋めた後も残すこと）

- AGENTS.mdの安全ルール1（破壊的コマンド）は、Claude Codeでは `.claude/settings.json` の `permissions.deny` と `.claude/hooks/deny_dangerous_bash.py`（PreToolUse hook）により**強制**される。hookの検出パターンを変更したら `python3 .claude/hooks/test_deny_dangerous_bash.py` で回帰テストを必ず実行する。
- AGENTS.mdのランブック（safe-rollback / go-live-checklist / project-health-check）は、Claude Codeではスキルとして自動発動する。「公開して」と言われても go-live-checklist の監査を通さずにデプロイへ進まない。「壊れた」と言われたら safe-rollback に従い、`git reset --hard` や force push で回復しない。
- テンプレートリポジトリでは `.github/workflows/verify-template.yml` が安全網の整合性（`scripts/verify_safety_net.py`）を毎push検査する。