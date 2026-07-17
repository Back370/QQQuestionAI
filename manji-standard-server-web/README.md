# manji-standard-server-web

manji-standard-server の **frontend** スタック(Next.js)。backend(Go/TS/Python)と同じ `proto` 契約から、
**型付き API クライアント + CRUD UI(list/detail/form)+ client validation** を生成する。

- 設計: **[DESIGN.md](./DESIGN.md)**(契約→UI 生成の全体設計)。
- ステータス: **CRUD 一式が動く Next.js アプリ**(2026-07-15)。契約から list/detail/form/delete + dynamic-route
  pages を生成し、`next build`(型検証込み)・`vitest`(コンポーネント)・codegen-drift CI まで **PASS**。
- 原則: 素の全列テーブル/全項目フォームにしない(データの壁回避)。優先度駆動レイアウトを生成側に組み込む。

## 使い方
```bash
npm install
npm run gen         # proto → src/gen/** + src/app/users/** を生成
npm run typecheck   # tsc --noEmit
npm run test        # vitest(生成物のコンポーネントテスト)
npm run build       # next build(本番ビルド + 型検証)
npm run dev         # next dev(要 backend: REST /users on :8080)
E2E_BASE_URL=http://localhost:3000 npm run e2e   # Playwright(アプリ+backend 起動時のみ)
```
- **生成**(`src/gen/`, `src/app/users/`, ヘッダ `// Code generated ... DO NOT EDIT.`):
  `types/user.ts`(型) / `client/user.ts`(@http由来の型付きfetch) / `ui/user/UserList.tsx`(優先度カードリスト) /
  `ui/user/UserDetail.tsx`(progressive disclosure) / `ui/user/UserForm.tsx`(create+edit, @required/@email 由来の validation) /
  `ui/user/UserDeleteButton.tsx` / `app/users/{page,new,[id],[id]/edit}`。
- **手書き**(生成物ではない外枠): `src/app/layout.tsx`(ルートレイアウト) / `src/app/page.tsx`(ホーム) /
  `src/lib/api.ts`(fetch基盤) / `src/lib/widgets.tsx`(componentMap=見た目)。
- **テスト**: `tests/component/`(vitest+RTL, client を mock) / `tests/e2e/`(Playwright, backend 必要なので既定 skip)。
- **CI**: `.github/workflows/web-ci.yml`(build-test + codegen-drift ゲート)。

関連ナレッジ(`~/knowledge_base`): [[contract-to-ui-codegen]] / [[ref-schema-driven-ui]] / [[selection-design-pattern]]。
