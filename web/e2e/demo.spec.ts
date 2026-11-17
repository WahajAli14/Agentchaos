import { expect, test } from "@playwright/test";

// Records the README demo. Run with `make demo`; excluded from the normal e2e run.
test.use({ video: { mode: "on", size: { width: 1440, height: 900 } }, viewport: { width: 1440, height: 900 } });

test("@demo failure, explanation, fix, benchmark", async ({ page }) => {
  const pause = (ms = 1400) => page.waitForTimeout(ms);
  await page.goto("/scenarios/timeout_after_commit");
  await pause();
  await page.getByLabel("Step the fault hits").selectOption("create_invoice");
  await pause(800);
  await page.getByRole("button", { name: "Run scenario" }).click();
  await expect(page.getByTestId("run-detail")).toBeVisible();
  await pause(2200);
  await page.getByTestId("mismatch").scrollIntoViewIfNeeded();
  await pause(3200);

  await page.goto("/scenarios/timeout_after_commit");
  await page.getByLabel("Step the fault hits").selectOption("create_invoice");
  await page.getByLabel(/verify_after_write/).check();
  await pause(1000);
  await page.getByRole("button", { name: "Run scenario" }).click();
  await expect(page.getByTestId("run-detail").locator('[data-outcome="verified_recovery"]')).toBeVisible();
  await pause(2600);

  await page.goto("/benchmark?batch=batch_000001");
  await page.waitForSelector(".recharts-bar-rectangle");
  await pause(3500);
});
