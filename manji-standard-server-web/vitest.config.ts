import { defineConfig } from "vitest/config";

// component test: jsdom + esbuild automatic JSX(tsconfig の jsx:preserve は Next 用なので上書き)。
export default defineConfig({
  esbuild: { jsx: "automatic" },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./tests/setup.ts"],
    include: ["tests/component/**/*.test.{ts,tsx}"],
  },
});
