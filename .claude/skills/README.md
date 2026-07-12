# Skills

このディレクトリには、このリポジトリ専用のClaude Codeスキル（`SKILL.md`）を置く。各スキルはサブディレクトリとして配置し、`<skill-name>/SKILL.md` にfrontmatter（`name`, `description`）と手順を記述する。

スキルの作成・改善・評価には `skill-creator` スキル（`/example-skills:skill-creator`）を使うと良い。

## 一覧

| スキル | 概要 |
|---|---|
| [claude-project-setup](claude-project-setup/SKILL.md) | プロジェクトにClaude Code用の`.claude/`環境（権限・hooks・プラグイン設定、CLAUDE.md、必要に応じたskill/command/agentの雛形）を対話形式でセットアップする。エンジニアが常駐しない組織向けの保守的な権限設計・セキュリティレビュー体制に加え、GitHub利用時はCIワークフロー・Dependabot・ブランチ保護の整備にも対応。 |
| [safe-rollback](safe-rollback/SKILL.md) | 「壊れた」「元に戻したい」となったときの復旧ワークフロー。破壊的コマンドを使わず、退避→切り分け→revert/切り戻しの順で安全に回復する。デプロイ・DBマイグレーションの巻き戻しにも対応。 |
| [go-live-checklist](go-live-checklist/SKILL.md) | アプリを公開・リリースする前の監査。リスクレベルを判定し、秘密情報・認証認可・露出面・データ運用を棚卸しして `/security-review` まで実行。高リスク用途では人間の専門家レビューを推奨する。 |
| [project-health-check](project-health-check/SKILL.md) | 「健康診断して」で発動する定期点検。Dependabot PR・セキュリティアラート・CI失敗・依存の脆弱性・放置ブランチを棚卸しし、優先度付きで報告。週1回の実行を推奨。 |
| [import-skills](import-skills/SKILL.md) | 「このリポジトリのスキルを入れて」「/import-skills」で発動。外部Gitリポジトリ（任意の公開リポジトリを含む）からスキルを取り込む。信用できないコード前提で秘密情報・実行コード/hook・プロンプトインジェクション・安全網との衝突を監査し、危険なものは人間の確認を得るまでコピーしない。 |

新しいスキルを追加したら、この表にも1行追記すること。frontmatterの `description` は全スキル分が毎セッションのコンテキストに常時読み込まれるので、トリガー条件（いつ発動すべきか）に絞って書き、手順や説明は本文に書くこと——スキルが増えるほどdescriptionの肥大が固定コストとして効いてくる。
