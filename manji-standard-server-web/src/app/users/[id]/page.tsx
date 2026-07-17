// Scaffolded by mss-protoc-gen — 編集可(アプリ所有)。再生成では上書きされない。

import { UserDetail } from "../../../gen/ui/user/UserDetail";
import { Page } from "../../../lib/widgets";

export default function UserDetailPage({ params }: { params: { id: string } }) {
  return (<Page title="User"><UserDetail id={params.id} /></Page>);
}
