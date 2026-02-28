import { test, expect } from '@playwright/test';
import { SEL } from '../helpers/selectors';
import { BUCKETS } from '../helpers/test-data';
import { dismissWelcomeIfPresent, navigateToBucket } from '../helpers/wait-helpers';

test.describe('Storage Dashboard', () => {
  test('9 opens dashboard from bucket view header button', async ({ page }) => {
    await page.goto('/');
    await dismissWelcomeIfPresent(page);
    await navigateToBucket(page, BUCKETS.MAIN);

    await page.locator(SEL.dashboardButton).click();
    await expect(page.locator(SEL.dashboardModal)).toBeVisible();
  });

  test('9 shows summary cards (Total Objects, Total Size)', async ({ page }) => {
    await page.goto('/');
    await dismissWelcomeIfPresent(page);
    await navigateToBucket(page, BUCKETS.MAIN);
    await page.locator(SEL.dashboardButton).click();

    const cards = page.locator(SEL.dashboardCard);
    await expect(cards.first()).toBeVisible();
    const count = await cards.count();
    expect(count).toBeGreaterThanOrEqual(2);

    // Check for Total Objects and Total Size labels
    await expect(page.locator(`${SEL.dashboardCardLabel}:has-text("Total Objects")`)).toBeVisible();
    await expect(page.locator(`${SEL.dashboardCardLabel}:has-text("Total Size")`)).toBeVisible();
  });

  test('9 shows storage by folder chart', async ({ page }) => {
    await page.goto('/');
    await dismissWelcomeIfPresent(page);
    await navigateToBucket(page, BUCKETS.MAIN);
    await page.locator(SEL.dashboardButton).click();

    // Bar chart rows should appear (at least root and subfolders)
    await expect(page.locator(SEL.dashboardBarRow).first()).toBeVisible({ timeout: 10_000 });
  });

  test('9 clicking folder label navigates to folder', async ({ page }) => {
    await page.goto('/');
    await dismissWelcomeIfPresent(page);
    await navigateToBucket(page, BUCKETS.MAIN);
    await page.locator(SEL.dashboardButton).click();

    // Wait for bar rows to load
    await expect(page.locator(SEL.dashboardBarRow).first()).toBeVisible({ timeout: 10_000 });

    // Click a folder label (not "(root files)")
    const folderLabel = page.locator(`${SEL.dashboardBarLabel}:not(:has-text("root"))`).first();
    if (await folderLabel.isVisible().catch(() => false)) {
      await folderLabel.click();
      // Dashboard should close and navigate to the folder
      await expect(page.locator(SEL.dashboardModal)).toBeHidden({ timeout: 5_000 });
    }
  });

  test('9 trend toggle button shows growth chart', async ({ page }) => {
    await page.goto('/');
    await dismissWelcomeIfPresent(page);
    await navigateToBucket(page, BUCKETS.MAIN);
    await page.locator(SEL.dashboardButton).click();

    await expect(page.locator(SEL.dashboardBarRow).first()).toBeVisible({ timeout: 10_000 });

    // Click trend toggle button
    const trendBtn = page.locator(SEL.trendToggleBtn).first();
    if (await trendBtn.isVisible().catch(() => false)) {
      await trendBtn.click();
      // Should show trend chart (SVG/img) or "Not enough data" message
      await page.waitForFunction(() => {
        const modal = document.querySelector('.dashboard-modal');
        if (!modal) return false;
        return modal.querySelector('.trend-chart-svg') !== null
          || modal.textContent?.includes('Not enough data');
      }, { timeout: 5_000 });
    }

    await page.locator(SEL.modalCloseButton).click();
  });

  test('9 close button works', async ({ page }) => {
    await page.goto('/');
    await dismissWelcomeIfPresent(page);
    await navigateToBucket(page, BUCKETS.MAIN);
    await page.locator(SEL.dashboardButton).click();
    await expect(page.locator(SEL.dashboardModal)).toBeVisible();

    await page.locator(SEL.modalCloseButton).click();
    await expect(page.locator(SEL.dashboardModal)).toBeHidden();
  });
});
