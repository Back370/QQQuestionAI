import { test, expect } from "@playwright/test";

// 生成された CRUD の通し(list → new → detail → edit → delete)。
// backend(REST /users)が必要なため E2E_BASE_URL が無い環境では丸ごとスキップ。
const enabled = !!process.env.E2E_BASE_URL;

test.describe("User CRUD (generated)", () => {
  test.skip(!enabled, "E2E_BASE_URL 未設定(アプリ+backend が必要)");

  test("create → appears in list → edit → delete", async ({ page }) => {
    const email = `e2e+${Date.now()}@example.com`;

    await page.goto("/users/new");
    await page.getByLabel("Email").fill(email);
    await page.getByLabel("Name").fill("E2E User");
    await page.getByRole("button", { name: "Create" }).click();

    await expect(page).toHaveURL(/\/users$/);
    await expect(page.getByText(email)).toBeVisible();

    await page.getByText(email).click();
    await page.getByRole("link", { name: "Edit" }).click();
    await page.getByLabel("Name").fill("E2E User (edited)");
    await page.getByRole("button", { name: "Update" }).click();
    await expect(page).toHaveURL(/\/users$/);

    await page.getByText(email).click();
    page.on("dialog", (d) => d.accept());
    await page.getByRole("button", { name: "Delete" }).click();
    await expect(page).toHaveURL(/\/users$/);
    await expect(page.getByText(email)).toHaveCount(0);
  });
});
