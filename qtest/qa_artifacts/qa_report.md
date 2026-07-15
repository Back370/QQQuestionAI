# QQQuestionAI QA Flow Report

実行時刻: 2026-07-13T04:57:48.240088+00:00
対象: /Users/back/back-projects/QQQuestionAI
ログ: /Users/back/back-projects/QQQuestionAI/qtest/qa_artifacts/flow_log.jsonl
総合判定: PASS

## シナリオ結果

| 問題 | 種別 | 操作 | 期待 | 実際 | 結果 |
| --- | --- | --- | --- | --- | --- |
| q1 | prerequisite | answer | correct | correct | PASS |
| q2 | prerequisite | hint | - | hint | PASS |
| q2 | prerequisite | answer | correct | correct | PASS |
| q3 | implementation | answer | incorrect | incorrect | PASS |
| q3 | implementation | hint | - | hint | PASS |
| q3 | implementation | answer | correct | correct | PASS |
| q4 | implementation | answer | incorrect | incorrect | PASS |
| q4 | implementation | hint | - | hint | PASS |
| q4 | implementation | answer | correct | correct | PASS |
| q5 | implementation | hint | - | hint | PASS |
| q5 | implementation | answer | incorrect | incorrect | PASS |
| q5 | implementation | giveup | incorrect | incorrect | PASS |

## 妥当性評価

- 5問完走: PASS
- 出題順序: PASS
- 前提知識2問 + 実装説明3問: PASS (prerequisite, prerequisite, implementation, implementation, implementation)
- スクリプト期待判定一致: 8/8

## 正確性評価

- オフライン判定精度: 5/5 (100%)
- 初回正答率: 40%
- 最終正答率: 80%
- ヒント有効率: 75%
- 判定失敗: []

## セキュリティ評価

- 問題表示での答え漏洩: PASS
- 未完了レスポンスでの答え漏洩: PASS
- ヒントでの答え漏洩: PASS
- ログ内の秘密情報らしき文字列: 0
- 実行方式: in-process FastAPI TestClient + InMemoryKnowledgeBase

## セッション集計

- 試行問題数: 5
- ヒント提示数: 4
- 答え漏洩率: 0%
- 解説の根拠被覆率: 57%
- 苦手傾向メモ: RNN, クロスエントロピー, 勾配計算, 誤差逆伝播

## 不確実性・制約

- FakeLLM による決定的QAであり、Gemini実APIの品質や外部検索結果の揺れは評価対象外。
- VSCode Webviewの表示確認、git hook経由の実コミット連携、実ネットワーク検索は今回の自動QAには含めていない。
