# QQQuestionAI ストリーミングQAレポート

総合判定: PASS
実行時刻: 2026-07-13T04:59:26.948814+00:00
対象: /Users/back/back-projects/QQQuestionAI
ログ: /Users/back/back-projects/QQQuestionAI/qtest/qa_artifacts_stream/stream_flow_log.jsonl

## SSEシナリオ結果

| 問題 | 操作 | 期待 | 実際 | SSEイベント | 判定 |
| --- | --- | --- | --- | --- | --- |
| q1 | answer_stream | correct | correct | judgement_partial/judgement_partial/judgement_partial/judgement_partial/judgement_partial/judgement_partial/judgement/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/result | PASS |
| q2 | hint | - | - | - | PASS |
| q2 | answer_stream | incorrect | incorrect | judgement/result | PASS |
| q2 | answer_stream | correct | correct | judgement_partial/judgement_partial/judgement_partial/judgement_partial/judgement_partial/judgement_partial/judgement/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/result | PASS |
| q3 | answer_stream | partial | partial | judgement_partial/judgement_partial/judgement_partial/judgement_partial/judgement_partial/judgement_partial/judgement/result | PASS |
| q3 | answer_stream | correct | correct | judgement_partial/judgement_partial/judgement_partial/judgement_partial/judgement_partial/judgement_partial/judgement/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/result | PASS |
| q4 | answer_stream | incorrect | incorrect | judgement/result | PASS |
| q4 | hint | - | - | - | PASS |
| q4 | answer_stream | correct | correct | judgement_partial/judgement_partial/judgement_partial/judgement_partial/judgement_partial/judgement_partial/judgement/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/result | PASS |
| q5 | answer_stream | incorrect | incorrect | judgement/result | PASS |
| q5 | giveup_stream | incorrect | incorrect | judgement/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/explanation_partial/result | PASS |

## 妥当性(ストリーミング固有)

- スクリプト期待判定一致: 9/9
- SSE Content-Type text/event-stream: PASS
- 判定→解説のイベント順序: PASS
- 不正解時に理由/解説の途中経過を流さない: PASS

## 正確性

- 実eval_set.json 判定精度: 5/5 (100%)
- 判定失敗: []
- ストリーム/非ストリーム判定一致: PASS
- ストリーム/非ストリーム模範解答一致: PASS

## セキュリティ(値ベース漏洩検出)

- 問題完了前の模範解答(本文)の漏洩: 0 件
- 問題完了前の accepted_points(要点)の漏洩: 0 件
- 漏洩詳細(要点): []

## 不確実性・制約

- FakeLLM 決定的判定であり、Gemini実APIのストリーム挙動・品質は対象外。
- 値ベース検出は正規化包含近似のため、極端に短い要点は誤検知回避のため min_len で除外。
- VSCode Webview 実表示・実SSEネットワークは対象外(HTTPレイヤの契約のみ検証)。
