import { defineConfig } from "@playwright/test";

// E2E は「アプリ + backend が動いている」前提。CI では既定でスキップ(E2E_BASE_URL 未設定時)。
// ローカル: `npm run build && npm start` の後 `E2E_BASE_URL=http://localhost:3000 npm run e2e`。
export default defineConfig({
  testDir: "./tests/e2e",
  use: {
    baseURL: process.env.E2E_BASE_URL || "http://localhost:3000",
  },
});
