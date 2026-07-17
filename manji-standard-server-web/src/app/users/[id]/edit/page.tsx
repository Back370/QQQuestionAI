// Scaffolded by mss-protoc-gen — 編集可(アプリ所有)。再生成では上書きされない。

import { UserForm } from "../../../../gen/ui/user/UserForm";
import { Page } from "../../../../lib/widgets";

export default function EditUserPage({ params }: { params: { id: string } }) {
  return (<Page title="Edit User"><UserForm id={params.id} /></Page>);
}
