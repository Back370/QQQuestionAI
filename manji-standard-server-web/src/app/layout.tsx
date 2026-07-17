// 手書き: App Router ルートレイアウト(生成物ではない — アプリの外枠)。
// tokens.css をここで読み込む(design tokens は全画面共通)。
import type { ReactNode } from "react";
import "../lib/tokens.css";

export const metadata = {
  title: "manji-standard-server-web",
  description: "契約(proto)→UI 生成のフロントエンド",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
