// Scaffolded by mss-protoc-gen — 編集可(アプリ所有)。再生成では上書きされない。

import Link from "next/link";
import { UserList } from "../../gen/ui/user/UserList";
import { Page } from "../../lib/widgets";

export default function UsersPage() {
  return (<Page title="Users" actions={<Link href="/users/new">+ New</Link>}><UserList /></Page>);
}
