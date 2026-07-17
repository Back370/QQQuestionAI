# 契約(proto)の単一の真実 — CONTRACT

このモノレポは **`/proto`** を全スタック共通の契約(single source of truth)とする。
`message` / `service` / `@annotation`(`@entity @pk @unique @email @required @timestamp @paging @http`)が
Go / TS-Hono / TS-Next / Python で同一であることを保証する。

## 方針
- **契約を変えるときは `/proto` を編集**し、各スタックの `proto/` に反映する。
- 言語固有オプション(`option go_package` 等)は**契約ではない**。各スタック側で持つ(Go は inline、
  将来的には buf managed mode に寄せるのが理想)。ドリフト検査はこれらを無視する。
- 各スタックは自分の `proto/` から自分の生成器で全レイヤを生成する(Go/TS = mss-protoc-gen、
  Python = tools/mss-protoc-gen の移植、`# Code generated ... DO NOT EDIT.`)。

## ドリフト検査(CI ゲート)
```bash
./scripts/check-contract.sh   # 各スタックの proto を /proto と比較(言語optionは無視)
```
全スタックで契約が一致していれば `contract unified across all stacks.` を出し exit 0。
不一致なら `DRIFT` を出し exit 非ゼロ(= 契約が割れている)。

## 現状(2026-07-15)
- `user` ドメイン: Go / TS-Hono / TS-Next / Python の契約は**一致**(差分は Go の `option go_package` のみ=言語束縛)。
- Python スタックを追加(FastAPI, `manji-standard-server-python/`)。生成器・migration生成・単体/結合テストまで実装。
