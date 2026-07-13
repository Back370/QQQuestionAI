# QQQuestionAI

コミット前に、いま書いたコードの差分から記述式5問（前提知識2問＋実装説明3問）を出題し、
「本当に分かって実装しているか」を確認する学習支援AIです。
答えは教えず、段階的なヒントだけを提示します。

## 前提

この拡張は同リポジトリの Python バックエンド（`backend/`）を起動して動作します。

1. `backend/` で venv を作成し依存をインストールする（リポジトリの CLAUDE.md 参照）
2. `backend/.env` に `GOOGLE_API_KEY` を設定する（`backend/env.example` をコピー）。
   API キーなしで試す場合は設定 `qqquestion.fakeLlm` を有効にする
3. コマンド「QQQuestionAI: pre-commit フックをインストール」を対象リポジトリで実行する
   （pre-commit フックの導入と `-q` 検知シェル関数の `~/.zshrc` への追記まで自動で行われる。
   反映に一度 `source ~/.zshrc` が必要）

## 使い方

1. コードを書いて `git add` する
2. ターミナルで `git commit -q -m "..."` を実行する
3. VSCode にクイズパネルが開くので、5問に記述式で解答する
   （不正解なら「ヒント」、降参は「ギブアップ」）
4. 5問完走するとコミットが続行される。パネルを閉じるとコミットは中止される

## 設定

| 設定 | 既定値 | 説明 |
|---|---|---|
| `qqquestion.pythonPath` | (自動) | バックエンド起動に使う Python |
| `qqquestion.port` | 8756 | バックエンドのポート |
| `qqquestion.fakeLlm` | false | API キー不要のデモモード |
