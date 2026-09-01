import { expect, test } from "@playwright/test";

const BACKEND = "http://127.0.0.1:8788";
const TOKEN = "public-flow-test-token";

test.setTimeout(180_000);
test.describe.configure({mode: "serial"});

test("the public service rejects a test path outside the authorized repository", async ({page}) => {
  await page.goto(`${BACKEND}/#token=${TOKEN}`);
  const status = await page.evaluate(async token => {
    const response = await fetch("/api/v1/reviews", {
      method: "POST",
      headers: {"Content-Type": "application/json", Authorization: `Bearer ${token}`},
      body: JSON.stringify({
        source: {kind: "local", repo_id: "unknown"},
        test_files: ["../outside.py"], declared_tests: [],
        goal: "g", budget_seconds: 30, model_provider: "deterministic",
      }),
    });
    return response.status;
  }, TOKEN);
  expect(status).toBe(400);
});

test("the public flow waits for approval and shows a named regression", async ({page}) => {
  await page.goto(`${BACKEND}/#token=${TOKEN}`);
  await expect(page.getByRole("button", {name: "评委模式"})).toHaveClass(/active/);
  await expect(page.locator("#preset")).toContainText("公开真实案例");
  await expect(page.getByLabel("运行边界")).toContainText("实时运行");

  await page.getByRole("button", {name: "一键运行案例"}).click();
  const approve = page.getByRole("button", {name: "确认计划并开始审查"});
  await expect(approve).toBeVisible({timeout: 60_000});
  await expect(page.getByLabel("评委结果概览")).toHaveCount(0);
  await approve.click();

  const overview = page.getByLabel("评委结果概览");
  await expect(overview).toBeVisible({timeout: 150_000});
  await expect(overview).toContainText("新增行");
  await expect(overview).toContainText("tests/test_calc.py::test_scaled");
  await expect(page.getByLabel("运行边界")).toContainText("覆盖优先");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});
