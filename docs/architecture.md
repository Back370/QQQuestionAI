# QQQuestionAI 実装方針 — コミット前・コード理解度チェック学習支援AI

direction.md を対象とした実装アーキテクチャを定義する。

---

## 1. コンセプト

- **解決したい課題**: 自分の書いたコードを「説明できるつもり」でも、第三者目線で質問されると答えられないことがある。コミット前に差分から出題し、「本当に分かって実装しているか」を判定する。
- **ペルソナ**: 「答えを絶対に教えず、ヒントだけで理解に導く教師」
- **基本動作**: `git commit -q` → ステージ済み差分から記述式5問を生成（第1〜2問: 前提知識、第3〜5問: 実装が何をしているかの説明）→ ユーザー解答 → 正誤判定 → 不正解時はヒント提示（段階制御）→ 正解/ギブアップ時は解説 → 5問完走でレポートを表示しコミット続行
- **合否の扱い**: コミットの続行条件は「5問の完走」であり全問正解ではない。学習支援が目的であり、コミットを罰にしない。ギブアップ・不正解は苦手傾向として記録し、次回の出題に反映する。
- **AIエンジン**: Gemini API（LangChain 経由で呼び出し、モデルを差し替え可能にする）

---

## 2. 技術スタック

| 役割 | 採用技術 | 理由 |
|---|---|---|
| UI | VSCode 拡張（TypeScript + Webview） | direction.md の指定。エディタ内で完結する |
| 出題・判定・ヒント生成 | Python 3.11+ / LangChain（`langchain-google-genai`） | LangChain で Gemini API 等の裏側モデルを抽象化・制御できる |
| 拡張 ⇔ バックエンド通信 | FastAPI ローカル HTTP サーバ（127.0.0.1 固定） | 拡張(TS)とロジック(Python)の言語をまたぐ最も単純な接続 |
| ベクトルDB | ChromaDB（永続化モード） | ローカル完結、セットアップ不要 |
| Web検索 | Tavily Search API（LangChain ツール。キーが無い環境では DuckDuckGo Search にフォールバック） | 差分のトピックごとに知識ベースを構築する |
| 差分取得 | `git diff --cached` | コミット対象（ステージ済み）だけを出題対象にする |
| 履歴永続化 | JSON Lines（`data/history.jsonl`） | 追記のみで壊れにくく、集計が容易 |

---

## 3. 全体構成

```
┌───────────────── VSCode ─────────────────┐
│  拡張 (TypeScript)                        │
│   ├ Webview クイズパネル（記述式解答UI）  │
│   ├ HTTP クライアント                     │
│   └ hook インストーラ（初回セットアップ） │
└───────────────┬──────────────────────────┘
                │ localhost HTTP (JSON)
┌───────────────▼── Python バックエンド ───┐
│  server.py (FastAPI)                      │
│   ├ diff_analyzer.py   差分→トピック抽出  │
│   ├ knowledge_base.py  Web検索→ChromaDB   │
│   ├ question_gen.py    5問＋採点基準の生成│
│   ├ judge.py           正誤判定           │
│   ├ hint_gen.py        段階ヒント生成     │
│   ├ explainer.py       解説生成(引用付き) │
│   ├ learner_model.py   苦手傾向の記録     │
│   └ evaluator.py       評価レポート       │
└───────────────▲──────────────────────────┘
                │ 発動要求 (HTTP)
        git pre-commit hook  ←  `git commit -q`
```

### 発動方法（`git commit -q` 連携）

git のフックはコミット時のコマンドラインオプションを直接参照できない。そこで:

1. 拡張の初回セットアップで、git エイリアス（またはシェル関数）を案内・登録する。`-q` 付き commit を包んで環境変数 `QQQ_QUIZ=1` を立ててから本物の `git commit` を呼ぶ。
2. `pre-commit` フックは `QQQ_QUIZ=1` のときだけバックエンドへ発動要求を送り、クイズ完走（またはユーザーによるパネルの明示的な中断）まで待機する。完走なら exit 0 でコミット続行、中断なら exit 1 でコミット中止。
3. バックエンド未起動（VSCode 外からのコミット等）の場合は警告メッセージだけ出して素通しする。クイズが受けられないことを理由にコミットを壊さない。

### データフロー

1. `git commit -q` → フックが `POST /quiz/start` を呼ぶ
2. `diff_analyzer` が `git diff --cached` を取得し、変更ファイル・関数・キーワードからトピックを抽出（例: RNN / BPTT / クロスエントロピー / softmax）
3. `knowledge_base` がトピックごとに Web 検索 → ChromaDB に格納（既取得トピックはキャッシュ利用）
4. `question_gen` が差分＋`learner_model` の苦手傾向をもとに5問を生成。**各問に模範解答・許容解答・採点基準を同時生成**する
5. 拡張の Webview に1問ずつ表示 → ユーザーが記述式で解答
6. `judge` が採点基準に対して正誤判定（正解 / 部分的 / 不正解の3値）
7. 不正解なら `hint_gen` が知識ベースの引用付きで段階ヒントを提示。正解またはギブアップで `explainer` が引用付き解説を表示
8. 5問終了 → `evaluator` がレポート出力、`learner_model` が履歴を `history.jsonl` へ追記 → フックへ完走を通知しコミット続行

---

## 4. 知識ベースとハルシネーション抑制

### 4.1 方針の選定理由

モデルのファインチューニングは行わず、**Web 検索 + RAG** で知識を差し込む。理由:

- 出題対象のトピックはコミット差分ごとに変わるため、事前学習では追従できない
- ヒント・解説に出典 URL を添えられ、Groundedness を機械的に検証できる
- ローカル完結（ChromaDB）で追加インフラが不要

### 4.2 知識ベースの構築（学習データの取り込み）

1. `diff_analyzer` が差分から抽出したトピックごとに Web 検索（上位3〜5件）
2. ページ本文を抽出し、500字・オーバーラップ100字でチャンク分割
3. 埋め込みを計算して ChromaDB へ格納。メタデータに URL・タイトル・取得日時を保持（解説の出典表示に使う）
4. 同一トピックは再検索しない（TTL 30日のキャッシュ）

### 4.3 ハルシネーション抑制策

- **判定の自由生成を減らす**: 出題時に模範解答・許容解答・採点基準を確定させ、判定はユーザー解答と採点基準の照合として行う。判定時に「正解を思い出させる」ことをしない
- **引用の強制**: ヒント・解説は検索チャンクを根拠として渡し、`citations` フィールドを必須にする。引用元に存在しない主張を含めない指示をプロンプトに固定
- **答え漏洩チェック**: 生成されたヒントに模範解答（表記ゆれ含む）が含まれていないか文字列＋埋め込み類似で検査し、漏洩していたら再生成（最大3回）
- **temperature**: 判定 0.0 / 解説 0.2 / ヒント 0.3
- **判定理由の必須化**: 3値判定に判定理由を必ず添えさせ、理由が採点基準を参照していない場合は再判定する

---

## 5. Gemini API の IO 仕様

### 5.1 共通リクエスト

- LangChain の `ChatGoogleGenerativeAI` を使用し、全役割で **structured output（Pydantic モデル指定）** により出力を JSON に固定する
- 共通入力: `diff_context`（該当コード断片）、`topic`、`retrieved_chunks`（知識ベースからの引用候補）
- API キーは環境変数 `GOOGLE_API_KEY` から読む。コード・ログに書かない

### 5.2 役割別 IO

#### (a) 出題生成 `generate_question(diff, topics, learner_state) -> list[Question]`

```python
class Question(BaseModel):
    id: str
    type: Literal["prerequisite", "implementation"]  # 前提知識 / 実装の説明
    text: str
    code_snippet: str | None      # implementation 型は差分から該当箇所を引用
    model_answer: str             # 模範解答
    accepted_points: list[str]    # 正解に含まれるべき要点（許容解答）
    rubric: str                   # 採点基準
    topic: str
    difficulty: int               # 1〜3
```

5問構成（prerequisite ×2 → implementation ×3）を生成側で強制する。

#### (b) 正誤判定 `judge_answer(question, user_answer) -> Judgement`

```python
class Judgement(BaseModel):
    verdict: Literal["correct", "partial", "incorrect"]
    matched_points: list[str]     # 満たした要点
    missing_points: list[str]     # 欠けている要点
    reason: str                   # 採点基準を参照した判定理由
```

許容解答との文字列一致で決まる場合は LLM を呼ばずに確定する。

#### (c) ヒント生成 `generate_hint(question, user_answer, hint_level, chunks) -> Hint`

```python
class Hint(BaseModel):
    hint: str
    citations: list[str]          # 根拠チャンクの出典URL
```

生成後に答え漏洩チェックを通し、漏洩時は再生成。

#### (d) 解説生成 `generate_explanation(question, chunks) -> Explanation`

```python
class Explanation(BaseModel):
    explanation: str
    citations: list[str]
```

正解時・ギブアップ時の両方で提示。主張は `chunks` に存在する内容に限定する。

---

## 6. ヒントの段階制御

`learner_model.py` がセッションをまたいだ状態を保持する:

```python
@dataclass
class LearnerState:
    topic_scores: dict[str, float]   # トピック別正答率
    current_hint_level: int          # 1(抽象) 〜 4(ほぼ核心)
    attempt_count: int
    history: list[Interaction]       # 全対話ログ (data/history.jsonl で永続化)
```

コード理解度チェック向けのヒントレベル定義:

- **レベル1**: 概念・分野レベルの手がかり（「これは逆伝播の依存関係の話です」）
- **レベル2**: 関連事象との対比（「順伝播のループと何が違うか比べてみましょう」）
- **レベル3**: コード上の着眼点（「どの変数のどの添字を見るべきか」）
- **レベル4**: 選択肢化（3択提示）
- 同一問題への再要求ごとにレベル +1。当該トピックの正答率が高いユーザーにはレベル1から、低いユーザーにはレベル2から開始

### 苦手傾向の反映（ルールベース）

- `history.jsonl` からトピック別正答率を集計し、正答率 50% 未満のトピックを「苦手」と判定。次回セッションの出題で優先的に取り上げる
- 出題難易度も `topic_scores` に応じて選択（正答率 70% 超で difficulty +1）
- LLM に委ねず集計とルールで決定する（direction.md「あると良い機能」への対応）

---

## 7. 評価指標

`evaluator.py` で以下を自動計測し、セッション終了時（コミット続行の直前）にレポート出力する:

| 指標 | 定義 | 測定方法 |
|---|---|---|
| 答え漏洩率 | ヒント中に正解が含まれた割合 | 文字列検査ログの集計（再生成前の値） |
| 判定精度 | 正誤判定の正しさ | `data/eval_set.json`（正解/部分的/不正解/表記ゆれの4種 × 各問題）でオフライン評価 |
| Groundedness | 解説中の主張が引用元に存在する割合 | citations の claim を出典テキストに対し文字列/埋め込み類似で照合 |
| 学習到達度 | セッション内の正答率推移 | 初回正答率 vs ヒント後正答率、ヒントレベル別の正答率 |
| ヒント有効率 | ヒント提示後に正解に至った割合 | 履歴ログから集計 |

---

## 8. 実装順序

1. **コアループ（CLI で検証）**: diff → 出題 → 判定 → ヒント → 解説 を Python 単体で動かす。Webview より先にロジックを固める
2. **知識ベース**: Web 検索 → ChromaDB 格納 → 引用付きヒント/解説。答え漏洩チェックもここで入れる
3. **FastAPI ローカルサーバ化**: コアループを `/quiz/start` 等のエンドポイントに載せる
4. **VSCode 拡張**: Webview クイズパネル + HTTP クライアント + サーバ自動起動
5. **git 連携**: pre-commit フック + `-q` 検知エイリアスのセットアップ導線
6. **learner_model / evaluator**: 履歴永続化、苦手傾向の出題反映、セッションレポート
7. **オフライン評価**: `data/eval_set.json` を整備し、判定精度・答え漏洩率を回帰的に測定する
