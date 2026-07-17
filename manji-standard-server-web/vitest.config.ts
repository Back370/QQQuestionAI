import { defineConfig } from "vitest/config";

// component test: jsdom + automatic JSX。vitest 4 の変換器は esbuild ではなく oxc なので、
// jsx は oxc 側で明示する(tsconfig 依存にすると jsx 設定の変更でテストだけ壊れる)。
export default defineConfig({
  oxc: { jsx: { runtime: "automatic" } },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./tests/setup.ts"],
    include: ["tests/component/**/*.test.{ts,tsx}"],
  },
});
