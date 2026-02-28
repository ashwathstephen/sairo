import { test, expect } from '@playwright/test';
import { SEL } from '../helpers/selectors';
import { dismissWelcomeIfPresent, clickTab } from '../helpers/wait-helpers';

test.describe('Health Check', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/');
    await dismissWelcomeIfPresent(page);
  });

  test('14.1 opens HealthCheck modal from Health button', async ({ page }) => {
    await page.locator(SEL.healthButton).click();
    await expect(page.locator(SEL.modal)).toBeVisible();
  });

  test('14.1 shows system status banner', async ({ page }) => {
    await page.locator(SEL.healthButton).click();

    // Should show "All Systems Operational" or similar status
    const banner = page.locator(SEL.hcSysBanner);
    await expect(banner).toBeVisible({ timeout: 10_000 });
  });

  test('14.1 shows metrics (uptime, buckets, users)', async ({ page }) => {
    await page.locator(SEL.healthButton).click();

    // Metrics should be visible
    await expect(page.locator(`${SEL.modal} :has-text("Uptime")`).first()).toBeVisible({ timeout: 10_000 });
    await expect(page.locator(`${SEL.modal} :has-text("Buckets")`).first()).toBeVisible();
  });

  test('14.1 shows S3 connectivity', async ({ page }) => {
    await page.locator(SEL.healthButton).click();
    await expect(page.locator(`${SEL.modal} :has-text("S3")`).first()).toBeVisible({ timeout: 10_000 });
    await expect(page.locator(`${SEL.modal} :has-text("Connected")`).first()).toBeVisible();
  });

  test('14.1 shows database status', async ({ page }) => {
    await page.locator(SEL.healthButton).click();
    await expect(page.locator(`${SEL.modal} :has-text("Database")`).first()).toBeVisible({ timeout: 10_000 });
  });

  test('14.1 shows bucket index status table', async ({ page }) => {
    await page.locator(SEL.healthButton).click();
    // Table with bucket index status
    await expect(page.locator(`${SEL.modal} table, ${SEL.modal} .dashboard-table`).first()).toBeVisible({ timeout: 10_000 });
  });

  test('14.2 S3 Compatibility tab loads', async ({ page }) => {
    await page.locator(SEL.healthButton).click();
    await clickTab(page, 'S3 Compatibility');

    // Should show feature checks summary (e.g. "8/9 features supported")
    await expect(page.locator(`${SEL.modal} :has-text("features supported")`).first()).toBeVisible({ timeout: 15_000 });
  });

  test('14.2 S3 Compatibility tab shows feature grid', async ({ page }) => {
    await page.locator(SEL.healthButton).click();
    await clickTab(page, 'S3 Compatibility');

    // Grid of feature checks
    await expect(page.locator(SEL.hcGrid).or(page.locator(`${SEL.modal} table`)).first()).toBeVisible({ timeout: 15_000 });
  });

  test('14.2 Re-check button refreshes results', async ({ page }) => {
    await page.locator(SEL.healthButton).click();
    await clickTab(page, 'S3 Compatibility');

    // Wait for initial results to load
    await expect(page.locator(`${SEL.modal} :has-text("features supported")`).first()).toBeVisible({ timeout: 15_000 });

    const recheckBtn = page.locator(`${SEL.modal} button:has-text("Re-check"), ${SEL.modal} button:has-text("Recheck")`).first();
    if (await recheckBtn.isVisible().catch(() => false)) {
      await recheckBtn.click();
      // Should refresh and show results again
      await page.waitForTimeout(2000);
      await expect(page.locator(`${SEL.modal} :has-text("features supported")`).first()).toBeVisible({ timeout: 15_000 });
    }
  });
});
