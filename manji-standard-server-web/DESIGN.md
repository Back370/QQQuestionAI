# manji-standard-server-web — 契約(proto)→ UI 生成 設計

**同一の proto 契約から frontend を生成する**スタック。backend(Go/TS/Python)と同じ `proto/user/v1/user.proto` を
単一の真実とし、そこから **型付き API クライアント + CRUD UI(list / detail / form) + client validation** を生成する。
backend↔frontend は同じ `@http`/`@annotation` から生成されるため**構造的に整合**する(ドリフトしない)。

> ステータス: **生成器実装済み・1本通った**(2026-07-15)。self-contained な Node 生成器(buf/protoc不要=proto直パース)で
> types/client/UserList/UserForm/page を生成、`tsc --noEmit` PASS。List=優先度カードリスト、Form validation=注釈由来。
> 裏付け: 社内KB [[contract-to-ui-codegen]] / サーベイ [[ref-schema-driven-ui]]、UI原則 [[selection-design-pattern]]。

## 1. スタック
Next.js 14(App Router)+ React 18 + TanStack Query(データ取得)+ zod(注釈由来の client validation)。
型は buf-es(`@bufbuild/protobuf`)で proto から生成、または独自 ts 型生成。

## 2. 契約の拡張（UI アノテーション）
proto の既存注釈(`@entity @pk @unique @email @required @timestamp @http`)を UI スキーマとして使い、
**表示関心の注釈**を足す(manji 流の leading-comment DSL):

| 注釈 | 意味 |
|---|---|
| `@label "表示名"` | フィールドの表示ラベル |
| `@list primary\|secondary\|hidden` | **list/表の表示優先度**(見出し/従属/非表示)★ 優先度駆動・列挙回避の核 |
| `@form text\|email\|select\|textarea\|readonly\|hidden` | form の widget(未指定なら型+`@email`等から推論) |
| `@sortable` / `@searchable` | list の並べ替え/検索対象 |

**既定(注釈なし)でも優先度駆動**: `@pk`→readonly + list-secondary、最初の非PK string→list-primary、
`@timestamp`→readonly + list-secondary、その他→list-secondary + form text。**素の全列ダンプにしない**のが既定。

## 3. 生成レイヤ（entity ごと、proto から）
1. **API クライアント** `src/gen/client/user.ts`: `@http` の各 RPC に対応する型付き fetch(list/get/create/update/delete/bulk…)。
   path/query/body を `@http` から割り付け。**backend の route と同じ契約から生成**＝呼び出しが必ず一致。
2. **型** `src/gen/types/user.ts`: entity DTO + request/response(proto から)。`@timestamp`→`<name>_unix:number`。
3. **UI コンポーネント** `src/gen/ui/user/`:
   - `UserList.tsx` — **優先度カードリスト**(生の全列テーブルにしない): `@list primary` を突出、`secondary` を従属、`hidden` は出さない。
     行→detail リンク。`@searchable`→検索、`@sortable`→並べ替え。→ [[selection-design-pattern]]。
   - `UserDetail.tsx` — 詳細(progressive disclosure: 本質を先、二次は畳む。開示は3段以内 → [[ref-ui-information-design]])。
   - `UserForm.tsx` — create/edit フォーム: widget は型+`@form` から。**client validation は `@required/@email/@unique` から生成**
     (entity の `new()` 検証と同じ注釈＝**単一の真実で backend/frontend の validation が一致**)。submit→API クライアント。
   - `UserDeleteButton.tsx` — confirm + delete。
4. **ページ/ルーティング** `src/gen/app/users/`(Next.js App Router): `/users`(list) `/users/[id]`(detail)
   `/users/new` `/users/[id]/edit` — 上のコンポーネントを組むページを生成。
5. **Renderer + componentMap**(手書き lib): `FieldRenderer` + `componentMap` で生成 form/list が実 widget に委譲。
   → **構造は生成、見た目(widget実体・テーマ)は手書きで差し替え可能**(サーベイの renderer+componentMap 流儀)。

## 4. 生成 vs 手書き（manji 原則）
- **生成**: API クライアント / 型 / CRUD コンポーネント / ページ / validation スキーマ = 機械的部分。
- **手書き**: `componentMap`(widget 実体・スタイル)/ デザイントークン・テーマ / 業務固有画面 / proto の `@ui` 優先度注釈。
  → 見た目と UX は手が持ち、配線は生成。

## 5. デザイン思想の埋め込み（最重要の「なぜ」）
素朴な契約→UI 生成は「全列テーブル + 全項目フォーム」= **データの壁 / 純粋な列挙**を生み、[[selection-design-pattern]] の
「必要最小限 / 優先度駆動 / 列挙にしない」に**真っ向から反する**。よって原則を生成器に組み込む:
- list = 既定で優先度カードリスト(primary突出/secondary従属/hidden)。全列テーブルにしない。
- detail = progressive disclosure(本質先・残りは畳む、3段以内)。
- form = 編集可能フィールドのみ(pk/timestamp は readonly)、グルーピング。
- `@list`/`@form` で「何が大事か」を**契約に表現**できる=優先度が後付けでなく設計の一部。

## 6. 契約統一
`@http` が backend route と frontend client を同時に生む＝ドリフト不能。`scripts/check-contract.sh` の対象に `web` を追加する。

## 7. 実装計画（次の build-out）
- `tools/mss-protoc-gen`(frontend 版): proto descriptor + `@annotation`/`@ui` を読み(Python 版 parse.py と同方式=protoc descriptor)、
  テンプレ(eta 等)で client/types/ui/pages を生成。
- `src/lib/component-map.tsx`(手書き)+ `FieldRenderer`。
- デモ `/users` CRUD を1本通す → 単体(コンポーネント)+ E2E(Playwright)+ codegen-drift を CI に追加。
