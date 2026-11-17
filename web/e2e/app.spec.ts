import { expect, test, type Page } from "@playwright/test";

const SHOTS = "../docs/screenshots";

test.describe.configure({ mode: "serial" });

async function useTheme(page: Page, theme: "dark" | "light") {
  await page.addInitScript((value) => localStorage.setItem("theme", value), theme);
}

for (const theme of ["dark", "light"] as const) {
  test(`screenshots with real benchmark data (${theme})`, async ({ page, request }) => {
    await useTheme(page, theme);
    const runs = await (await request.get("/api/runs?batch_id=batch_000001")).json();
    const failing = runs.find(
      (r: { fault_type: string; controls: string[]; classification: string }) =>
        r.fault_type === "timeout_after_commit" && r.controls.length === 0 && r.classification === "undetected_violation",
    );
    expect(failing, "the benchmark must contain a reproduced failure").toBeTruthy();

    await page.goto(`/runs/${failing.run_id}?batch=batch_000001&fault=timeout_after_commit&controls=none`);
    await expect(page.getByTestId("run-detail")).toBeVisible();
    await expect(page.getByTestId("mismatch")).toBeVisible();
    await page.setViewportSize({ width: 1600, height: 1500 });
    await page.screenshot({ path: `${SHOTS}/run-detail-${theme}.png` });
    await page.setViewportSize({ width: 1440, height: 1000 });

    await page.goto("/benchmark?batch=batch_000001");
    await expect(page.getByTestId("batch-summary")).toContainText("360");
    await page.waitForSelector(".recharts-bar-rectangle");
    await page.setViewportSize({ width: 1600, height: 1750 });
    await page.screenshot({ path: `${SHOTS}/benchmark-${theme}.png` });
    await page.setViewportSize({ width: 1440, height: 1000 });

    await page.goto("/scenarios/timeout_after_commit");
    await expect(page.getByRole("button", { name: "Run scenario" })).toBeVisible();
    await page.screenshot({ path: `${SHOTS}/scenarios-${theme}.png` });

    await page.goto("/runs?batch=batch_000001");
    await expect(page.locator("tbody tr").first()).toBeVisible();
    await page.screenshot({ path: `${SHOTS}/runs-${theme}.png` });
  });
}

test("run a scenario from the UI, read its timeline, then the benchmark updates", async ({ page }) => {
  await page.goto("/");
  await expect(page).toHaveURL(/\/scenarios/);
  await page.getByRole("link", { name: /timeout_after_commit/ }).click();
  await page.getByLabel("Step the fault hits").selectOption("create_invoice");
  await page.getByRole("button", { name: "Run scenario" }).click();

  // Controls off: the failure is reproduced and explained on the timeline.
  await expect(page).toHaveURL(/\/runs\/run_\d+/);
  const detail = page.getByTestId("run-detail");
  await expect(detail).toBeVisible();
  await expect(detail.locator('[data-outcome="undetected_violation"]')).toBeVisible();
  await expect(detail.getByText("decisions and tool calls")).toBeVisible();
  await expect(detail.getByText("injected faults, real responses")).toBeVisible();
  await expect(detail.getByText("service databases", { exact: true })).toBeVisible();
  await expect(page.getByTestId("mismatch")).toContainText("agent was told: TIMEOUT");
  await expect(page.getByTestId("mismatch")).toContainText("billing.invoices");
  const failedUrl = page.url();

  // Same scenario with verify-after-write: verified from database state.
  await page.goto("/scenarios/timeout_after_commit");
  await page.getByLabel("Step the fault hits").selectOption("create_invoice");
  await page.getByLabel(/verify_after_write/).check();
  await page.getByRole("button", { name: "Run scenario" }).click();
  await expect(page.getByTestId("run-detail").locator('[data-outcome="verified_recovery"]')).toBeVisible();
  expect(page.url()).not.toBe(failedUrl);

  // A shared link opens the same run.
  await page.goto(failedUrl);
  await expect(page.getByTestId("run-detail").locator('[data-outcome="undetected_violation"]')).toBeVisible();

  // Benchmark: run a new matrix and see the screen switch to the new batch.
  await page.goto("/benchmark");
  const summary = page.getByTestId("batch-summary");
  await expect(summary).toContainText("batch_000001");
  await page.getByLabel("Seeds per cell").fill("2");
  await page.getByRole("button", { name: "Run matrix" }).click();
  await expect(summary).toContainText("36 runs", { timeout: 60_000 });
  await expect(summary).not.toContainText("batch_000001");

  // Each matrix cell links to the runs behind it.
  await page.locator("tbody td a").first().click();
  await expect(page).toHaveURL(/\/runs\?batch=.*fault=timeout_after_commit.*controls=none/);
  await expect(page.locator("tbody tr")).toHaveCount(2);
});

test("an unknown run shows the real error and a retry", async ({ page }) => {
  await page.goto("/runs/run_999999");
  await expect(page.getByRole("alert")).toContainText("HTTP 404");
  await expect(page.getByRole("button", { name: "Retry" })).toBeVisible();
});
