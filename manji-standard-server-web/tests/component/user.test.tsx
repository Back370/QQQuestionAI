// 生成物(UserList / UserForm)のコンポーネントテスト。client は mock。
// 目的: 契約→生成された UI が「優先度リスト表示」「@required/@email 検証」を満たすことの回帰保証。
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

// vi.mock はファイル先頭へ巻き上げられるため、参照する変数は vi.hoisted で先に生成する。
const m = vi.hoisted(() => ({
  push: vi.fn(),
  refresh: vi.fn(),
  listUsers: vi.fn(),
  getUser: vi.fn(),
  createUser: vi.fn(),
  updateUser: vi.fn(),
  deleteUser: vi.fn(),
}));

vi.mock("next/navigation", () => ({ useRouter: () => ({ push: m.push, refresh: m.refresh }) }));
vi.mock("../../src/gen/client/user", () => ({
  listUsers: m.listUsers,
  getUser: m.getUser,
  createUser: m.createUser,
  updateUser: m.updateUser,
  deleteUser: m.deleteUser,
}));

import { UserList } from "../../src/gen/ui/user/UserList";
import { UserForm } from "../../src/gen/ui/user/UserForm";

beforeEach(() => {
  Object.values(m).forEach((fn) => fn.mockReset());
});

describe("UserList", () => {
  it("renders the primary field(email) for each row", async () => {
    m.listUsers.mockResolvedValue([
      { id: "u1", email: "a@example.com", name: "Alice", created_at_unix: 1_700_000_000 },
      { id: "u2", email: "b@example.com", name: "Bob", created_at_unix: 1_700_000_100 },
    ]);
    render(<UserList />);
    expect(await screen.findByText("a@example.com")).toBeInTheDocument();
    expect(await screen.findByText("b@example.com")).toBeInTheDocument();
    // 全列テーブルにしない設計: name は primary/secondary に無いので出ない
    expect(screen.queryByText("Alice")).not.toBeInTheDocument();
  });
});

describe("UserForm (create)", () => {
  // 生成された client-side validation(@required/@email 由来)。
  // 注: email フィールドは <input type="email"> なので、"@" 無しの値はブラウザ(jsdom)の
  //     ネイティブ制約検証が submit 自体をブロックする(生成 JS 検証と二重の防御)。
  //     よって「フォーマット不正」経路は入力欄経由では到達不能。ここでは空値による
  //     @required 経路(JS 側 setError が発火する)を検証する。
  it("blocks submit and shows a required error when a required field is empty", async () => {
    render(<UserForm />);
    fireEvent.click(screen.getByRole("button", { name: "Create" }));
    expect(await screen.findByText(/is required/)).toBeInTheDocument();
    expect(m.createUser).not.toHaveBeenCalled();
  });

  it("submits and redirects on valid input", async () => {
    m.createUser.mockResolvedValue(undefined);
    render(<UserForm />);
    fireEvent.change(screen.getByLabelText("Email"), { target: { value: "a@example.com" } });
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Alice" } });
    fireEvent.click(screen.getByRole("button", { name: "Create" }));
    await waitFor(() => expect(m.createUser).toHaveBeenCalledWith({ email: "a@example.com", name: "Alice" }));
    expect(m.push).toHaveBeenCalledWith("/users");
  });
});

describe("UserForm (edit)", () => {
  it("prefills from getUser and calls updateUser", async () => {
    m.getUser.mockResolvedValue({ id: "u1", email: "a@example.com", name: "Alice", created_at_unix: 1 });
    m.updateUser.mockResolvedValue(undefined);
    render(<UserForm id="u1" />);
    await waitFor(() => expect((screen.getByLabelText("Email") as HTMLInputElement).value).toBe("a@example.com"));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Alice2" } });
    fireEvent.click(screen.getByRole("button", { name: "Update" }));
    await waitFor(() => expect(m.updateUser).toHaveBeenCalledWith("u1", { email: "a@example.com", name: "Alice2" }));
  });
});
