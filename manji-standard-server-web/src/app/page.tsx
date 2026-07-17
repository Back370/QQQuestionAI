// 手書き: ホーム(生成物ではない)。生成された各 entity 画面への入口。
import Link from "next/link";

export default function Home() {
  return (
    <main style={{ maxWidth: 640, margin: "2rem auto", fontFamily: "system-ui" }}>
      <h1>manji-standard-server-web</h1>
      <p>契約(proto)→UI 生成のデモ。</p>
      <ul>
        <li>
          <Link href="/users">Users</Link>
        </li>
      </ul>
    </main>
  );
}
