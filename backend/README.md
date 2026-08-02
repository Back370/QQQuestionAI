# backend

QQQuestionAI のバックエンド（Python 3.11+）。差分から出題し、判定・ヒント・解説を返す本体で、
VSCode 拡張・`quiz` コマンド・pre-commit フックはすべてこのローカルサーバ（既定 `127.0.0.1:8756`）の
クライアントにすぎない。モジュール構成は [../CLAUDE.md](../CLAUDE.md) と
[../docs/architecture.md](../docs/architecture.md) を参照。

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -e . fastapi uvicorn pytest httpx
.venv/bin/pip install langchain-core langchain-google-genai chromadb ddgs

.venv/bin/python -m pytest                                  # テスト（LLM/APIキー不要）
QQQ_FAKE_LLM=1 .venv/bin/python -m qqquestion.cli --demo    # APIキー不要のCLIデモ
GOOGLE_API_KEY=... .venv/bin/python -m qqquestion.server    # 単体でサーバを起動
```

---

## 配布版の拡張がバックエンドとAPIキーを掴んでしまう問題

**症状**: Marketplace からインストールした QQQuestionAI（`Back-Room.qqquestion-ai-v`）を無効化したのに、
開発中の拡張をF5で起動すると、直したはずのバックエンドの変更が反映されない。
APIキーやモデルの変更も効かない。

### なぜ起きるか

原因は3つあり、どれも「拡張を無効にする」だけでは消えない。

**1. ポート 8756 は先着1つだけ**

[extension.ts:161-165](../extension/src/extension.ts#L161-L165) の `startBackend` は、
`/health` が応答したら「もう起動している」と判断して**自分ではプロセスを立てない**。
つまり先に 8756 を掴んだ側が唯一のバックエンドになり、後から来た開発中の拡張は
**配布版のバックエンドにぶら下がる**。UIは新しいのに、出題・判定を実行しているのは古いコード、という状態になる。

**2. 「無効化」ではプロセスは止まらない**

- 無効化はウィンドウを再読み込みするまで効かない。**別ウィンドウ**（別プロジェクトを開いている VSCode）で
  有効なままなら、そちらの拡張ホストがバックエンドを起動し続ける。「ワークスペースで無効にする」を選んだ場合も同様。
- 拡張ホストが強制終了・クラッシュした場合、`spawn` した Python は**孤児プロセスとして生き残り**、
  ポートを掴んだままになる。

実際にこのマシンで観測した状態（PID は都度変わる）:

```
$ lsof -nP -iTCP:8756 -sTCP:LISTEN
python3.1 3432 back 7u IPv4 ... TCP 127.0.0.1:8756 (LISTEN)

$ lsof -a -p 3432 -d cwd            # どのソースで動いているか
/Users/back/.vscode/extensions/back-room.qqquestion-ai-v-1.0.0/bundled   ← 配布版1.0.0の同梱ソース
```

**3. 保存領域と鍵は「拡張ID」単位で共有される**

開発ホスト（`--extensionDevelopmentPath`）で動く拡張も、ID は配布版と同じ `back-room.qqquestion-ai-v`。
そのため以下が**配布版と共用**になる。

| 共有されるもの | 実体 | 影響 |
| --- | --- | --- |
| SecretStorage（APIキー） | OS キーチェーン | 開発側で入力し直さずに済む反面、どちらで変えても両方に効く |
| globalStorage の venv | `~/Library/Application Support/Code/User/globalStorage/back-room.qqquestion-ai-v/venv` | `requirements.txt` が両者で違うと、起動のたびに入れ直しが往復する |
| 履歴・ログ (`QQQ_DATA_DIR`) | 同上 `/data` | 開発中の試行が本番の履歴・苦手傾向に混ざる |
| ターミナル用 `quiz` shim | 同上 `/bin/quiz` | ファイルは1つしかなく、**ポートを焼き込んで**最後に書いた側が勝つ |

APIキーは**プロセス起動時の環境変数**としてバックエンドに渡る（[extension.ts:200-205](../extension/src/extension.ts#L200-L205)）。
すでに動いているバックエンドには後から反映できないため、キーを変えても
「このウィンドウ以外が起動したバックエンドが動いています」という警告
（[extension.ts:275-283](../extension/src/extension.ts#L275-L283)）が出るだけで終わる。**これが出たら本問題を疑う。**

### 解決手順

#### 手順1: いま 8756 を掴んでいるものを止める

```bash
lsof -nP -iTCP:8756 -sTCP:LISTEN                 # PID を確認
lsof -a -p <PID> -d cwd                          # どのソースで動いているか確認
kill <PID>                                       # 止める（-9 は不要）
```

`cwd` が `~/.vscode/extensions/...` なら配布版、`.../QQQuestionAI/backend` なら開発版、
`.../globalStorage/.../bundled` 相当なら同梱ソースが動いている。

#### 手順2: 配布版を「無効化」ではなくアンインストールする（推奨）

開発中は共存させないのが一番確実。古い publisher の版が残っていることもあるので両方消す。

```bash
code --uninstall-extension Back-Room.qqquestion-ai-v
code --uninstall-extension qqquestion.qqquestion-ai   # 旧ID（0.1.0）。入っていれば
code --list-extensions | grep -i qqq                  # 空になったことを確認
```

その後、**開いているすべての VSCode ウィンドウを再読み込み**する（コマンドパレット → Developer: Reload Window）。
再読み込みしないと、実行中の拡張ホストは古い状態のまま動き続ける。

配布版を使い続けたい（普段使いを止めたくない）場合は、アンインストールせず手順3の**別ポート**で共存させる。

#### 手順3: 開発ホストを隔離する

[.vscode/launch.json](../.vscode/launch.json) には `--disable-extensions` を入れてある。
これで**開発ホスト側**では配布版が起動しなくなる（ただし他ウィンドウの配布版は止まらないので、
共存させるなら下のポート分離が要る）。

開発ホストで開くテスト用リポジトリの `.vscode/settings.json` に、開発専用の設定を置く:

```jsonc
{
  // 配布版(8756)とぶつからない開発専用ポート
  "qqquestion.port": 8757,
  // globalStorage の共用 venv を触らせない（requirements.txt を変えたときの入れ直し往復を防ぐ）
  "qqquestion.pythonPath": "/絶対パス/QQQuestionAI/backend/.venv/bin/python"
}
```

ユーザー設定（settings.json のグローバル側）に書くと配布版にも効いてしまうため、**必ずワークスペース設定**に置くこと。

## 最新の拡張機能をテストするフロー

1. **ビルドする**

   ```bash
   cd extension && npm install && npm run compile
   ```

   `npm run compile` は `scripts/bundle-backend.js` を走らせ、`backend/qqquestion/` を
   `extension/bundled/` に複製してから tsc する。**`backend/` を直したら毎回これを実行する**
   （F5 の `preLaunchTask` でも走る）。

2. **8756 が空いていることを確認する**（手順1）。`lsof` が何も返さないのが正しい状態。

3. **F5 で拡張開発ホストを起動する**（構成「拡張機能を実行 (Extension Development Host)」）。

4. **開発ホストでテスト用リポジトリを開く**。どの Python ソースが使われるかはここで決まる
   （[extension.ts:52-64](../extension/src/extension.ts#L52-L64)）。

   - ワークスペースに `backend/qqquestion/server.py` があれば**そのソース**（＝リポジトリを直接編集して試せる）
   - 無ければ `extension/bundled/`（＝ `npm run compile` した内容。配布版に近い経路を検証できる）

   VSCode は同じフォルダを2つのウィンドウで開けないため、通常は後者になる。前者で試したい場合は
   `git worktree` で別ディレクトリを作り、それを開発ホストで開く。

5. **どのバックエンドに繋がったかを必ず確認する**。出力パネル「QQQuestionAI」に出る次の行を見る:

   ```
   バックエンドを起動: <python> -m qqquestion.server (cwd=<ここが今回のソース>)
   ```

   代わりに `バックエンドは既に起動しています` と出ていたら、**既存プロセスに相乗りしている**＝
   テストになっていない。手順1に戻る。

6. **APIキーとモデルを設定する**（コマンドパレット）。

   - `QQQuestionAI: API キーを設定` → 保存後にバックエンドが再起動される
   - `QQQuestionAI: 使用するモデルを選択` → 起動中のバックエンドに即時反映（`POST /models/select`）

7. **クイズを動かす**。`git add` してから `QQQuestionAI: クイズを開始`。

8. **ターミナルの `quiz` を試す場合**はポートを明示する。shim は共用ファイルで、
   焼き込まれたポートが配布版のものである可能性があるため:

   ```bash
   QQQ_PORT=8757 quiz          # shim は既存の QQQ_PORT を尊重する
   ```

9. **後片付け**: 開発ホストを閉じたら `lsof -nP -iTCP:8757 -sTCP:LISTEN` で残骸が無いか確認し、
   配布版を使い続けるなら再インストールする。

### 確認用コマンドまとめ

```bash
# 誰がポートを掴んでいるか / そのプロセスの実体
lsof -nP -iTCP:8756 -sTCP:LISTEN
ps -eo pid,ppid,etime,command | grep qqquestion.server | grep -v grep
lsof -a -p <PID> -d cwd

# バックエンドの生死とモデル
curl -s http://127.0.0.1:8756/health   # {"status":"ok","kb_chunks":N,...}

# インストール済み拡張
code --list-extensions | grep -i qqq

# 共有されている領域の中身（venv / data / bin の実体）
ls "$HOME/Library/Application Support/Code/User/globalStorage/back-room.qqquestion-ai-v"
```

生成に失敗したときのログは `QQQ_DATA_DIR/server.log`。
`backend/` のソースから起動した場合は `backend/data/server.log`、
同梱ソースから起動した場合は上の globalStorage 配下の `data/server.log` にある。
