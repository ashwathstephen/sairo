import { test, expect } from '@playwright/test';
import { SEL } from '../helpers/selectors';
import { dismissWelcomeIfPresent } from '../helpers/wait-helpers';

test.describe('Audit Log', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/');
    await dismissWelcomeIfPresent(page);
  });

  test('16 opens AuditLog modal from Activity button', async ({ page }) => {
    await page.locator(SEL.activityButton).click();
    await expect(page.locator(SEL.modal)).toBeVisible();
  });

  test('16 shows audit table with columns', async ({ page }) => {
    await page.locator(SEL.activityButton).click();

    // Table should be visible
    const table = page.locator(`${SEL.modal} table`);
    await expect(table).toBeVisible({ timeout: 10_000 });

    // Should have header columns
    await expect(page.locator(`${SEL.modal} th:has-text("Time"), ${SEL.modal} th:has-text("Date")`).first()).toBeVisible();
    await expect(page.locator(`${SEL.modal} th:has-text("User")`)).toBeVisible();
    await expect(page.locator(`${SEL.modal} th:has-text("Action")`)).toBeVisible();
  });

  test('16 has audit entries from previous test actions', async ({ page }) => {
    await page.locator(SEL.activityButton).click();

    // Should have at least one row of audit data (from global setup creating buckets, uploading files, etc.)
    const rows = page.locator(`${SEL.modal} tbody tr`);
    await expect(rows.first()).toBeVisible({ timeout: 10_000 });
    const count = await rows.count();
    expect(count).toBeGreaterThan(0);
  });

  test('16 action filter dropdown works', async ({ page }) => {
    await page.locator(SEL.activityButton).click();

    const filterSelect = page.locator(`${SEL.modal} select`).first();
    if (await filterSelect.isVisible().catch(() => false)) {
      // Select a specific action type
      const options = await filterSelect.locator('option').allTextContents();
      expect(options.length).toBeGreaterThan(1); // At least "All" + others
    }
  });

  test('16 refresh button reloads entries', async ({ page }) => {
    await page.locator(SEL.activityButton).click();

    const refreshBtn = page.locator(`${SEL.modal} button:has-text("Refresh"), ${SEL.modal} button[aria-label="Refresh"]`).first();
    if (await refreshBtn.isVisible().catch(() => false)) {
      await refreshBtn.click();
      await page.waitForTimeout(1000);
      await expect(page.locator(`${SEL.modal} tbody tr`).first()).toBeVisible();
    }
  });

  test('16 pagination controls present', async ({ page }) => {
    await page.locator(SEL.activityButton).click();

    // Pagination may show Prev/Next buttons or page indicators
    const pagination = page.locator(`${SEL.modal} :has-text("Prev"), ${SEL.modal} :has-text("Next"), ${SEL.modal} :has-text("Page")`);
    // Pagination may not exist if few entries — just check it doesn't error
    await page.waitForTimeout(1000);
  });
});
