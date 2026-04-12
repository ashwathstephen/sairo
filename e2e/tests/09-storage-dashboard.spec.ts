import { test, expect } from '@playwright/test';
import { SEL } from '../helpers/selectors';
import { BUCKETS } from '../helpers/test-data';
import { dismissWelcomeIfPresent, navigateToBucket } from '../helpers/wait-helpers';

test.describe('Insights Modal — Storage Tab', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/');
    await dismissWelcomeIfPresent(page);
    await navigateToBucket(page, BUCKETS.MAIN);
  });

  test('9.1 opens Insights modal from header button', async ({ page }) => {
    await page.locator(SEL.dashboardButton).click();
    await expect(page.locator(SEL.dashboardModal)).toBeVisible();
    // Title should say "Insights"
    await expect(page.locator('.dashboard-modal h2')).toContainText('Insights');
  });

  test('9.2 shows Storage and Optimize tab buttons', async ({ page }) => {
    await page.locator(SEL.dashboardButton).click();
    await expect(page.locator(SEL.insightsStorageTab)).toBeVisible();
    await expect(page.locator(SEL.insightsOptimizeTab)).toBeVisible();
  });

  test('9.3 Storage tab is active by default', async ({ page }) => {
    await page.locator(SEL.dashboardButton).click();
    // Summary cards should be visible on Storage tab
    await expect(page.locator(`${SEL.dashboardCardLabel}:has-text("Total Objects")`)).toBeVisible();
    await expect(page.locator(`${SEL.dashboardCardLabel}:has-text("Total Size")`)).toBeVisible();
  });

  test('9.4 shows summary cards with values', async ({ page }) => {
    await page.locator(SEL.dashboardButton).click();
    const cards = page.locator(SEL.dashboardCard);
    await expect(cards.first()).toBeVisible();
    const count = await cards.count();
    // At least Total Objects and Total Size (cost cards appear if provider detected)
    expect(count).toBeGreaterThanOrEqual(2);
  });

  test('9.5 shows storage by folder chart', async ({ page }) => {
    await page.locator(SEL.dashboardButton).click();
    // Bar chart rows should appear (at least root and subfolders)
    await expect(page.locator(SEL.dashboardBarRow).first()).toBeVisible({ timeout: 10_000 });
  });

  test('9.6 clicking folder label navigates to folder', async ({ page }) => {
    await page.locator(SEL.dashboardButton).click();
    await expect(page.locator(SEL.dashboardBarRow).first()).toBeVisible({ timeout: 10_000 });

    // Click a folder label (not "(root files)")
    const folderLabel = page.locator(`${SEL.dashboardBarLabel}:not(:has-text("root"))`).first();
    if (await folderLabel.isVisible().catch(() => false)) {
      await folderLabel.click();
      // Dashboard should close and navigate to the folder
      await expect(page.locator(SEL.dashboardModal)).toBeHidden({ timeout: 5_000 });
    }
  });

  test('9.7 trend toggle button shows growth chart', async ({ page }) => {
    await page.locator(SEL.dashboardButton).click();
    await expect(page.locator(SEL.dashboardBarRow).first()).toBeVisible({ timeout: 10_000 });

    const trendBtn = page.locator(SEL.trendToggleBtn).first();
    if (await trendBtn.isVisible().catch(() => false)) {
      await trendBtn.click();
      // Should show trend chart (SVG) or "Not enough data" message
      await page.waitForFunction(() => {
        const modal = document.querySelector('.dashboard-modal');
        if (!modal) return false;
        return modal.querySelector('.trend-chart-svg') !== null
          || modal.textContent?.includes('Not enough data');
      }, { timeout: 5_000 });
    }
  });

  test('9.8 close button works', async ({ page }) => {
    await page.locator(SEL.dashboardButton).click();
    await expect(page.locator(SEL.dashboardModal)).toBeVisible();
    await page.locator(SEL.modalCloseButton).click();
    await expect(page.locator(SEL.dashboardModal)).toBeHidden();
  });
});

test.describe('Insights Modal — Optimize Tab', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/');
    await dismissWelcomeIfPresent(page);
    await navigateToBucket(page, BUCKETS.MAIN);
    await page.locator(SEL.dashboardButton).click();
    await expect(page.locator(SEL.dashboardModal)).toBeVisible();
  });

  test('9.9 switching to Optimize tab loads optimization data', async ({ page }) => {
    await page.locator(SEL.insightsOptimizeTab).click();

    // Should show spinner while loading, then content
    await page.waitForFunction(() => {
      const modal = document.querySelector('.dashboard-modal');
      if (!modal) return false;
      // Either loading spinner is gone and content appeared, or empty message is shown
      const hasSpinner = modal.querySelector('.spinner') !== null;
      const hasContent = modal.querySelector('h4') !== null;
      const hasEmpty = modal.textContent?.includes('empty') || modal.textContent?.includes('No optimization');
      const hasAllClear = modal.textContent?.includes('All clear');
      return !hasSpinner && (hasContent || hasEmpty || hasAllClear);
    }, { timeout: 30_000 });
  });

  test('9.10 Optimize tab shows recommendations or all-clear', async ({ page }) => {
    await page.locator(SEL.insightsOptimizeTab).click();

    // Wait for loading to finish
    await page.waitForFunction(() => {
      const modal = document.querySelector('.dashboard-modal');
      return modal && !modal.querySelector('.spinner');
    }, { timeout: 30_000 });

    // Should show either recommendations (h4 headings) or "All clear" message
    const hasRecommendations = await page.locator('.dashboard-modal h4').count() > 0;
    const hasAllClear = await page.locator('.dashboard-modal :has-text("All clear")').count() > 0;
    const hasEmpty = await page.locator('.dashboard-modal .muted:has-text("empty")').count() > 0;

    expect(hasRecommendations || hasAllClear || hasEmpty).toBeTruthy();
  });

  test('9.11 switching back to Storage tab preserves data', async ({ page }) => {
    // Switch to Optimize
    await page.locator(SEL.insightsOptimizeTab).click();
    await page.waitForFunction(() => {
      const modal = document.querySelector('.dashboard-modal');
      return modal && !modal.querySelector('.spinner');
    }, { timeout: 30_000 });

    // Switch back to Storage
    await page.locator(SEL.insightsStorageTab).click();

    // Storage cards should still be visible
    await expect(page.locator(`${SEL.dashboardCardLabel}:has-text("Total Objects")`)).toBeVisible();
  });

  test('9.12 Optimize tab shows accuracy disclaimer if recommendations exist', async ({ page }) => {
    await page.locator(SEL.insightsOptimizeTab).click();

    await page.waitForFunction(() => {
      const modal = document.querySelector('.dashboard-modal');
      return modal && !modal.querySelector('.spinner');
    }, { timeout: 30_000 });

    // If there are recommendations, there should be an accuracy note
    const hasRecommendations = await page.locator('.dashboard-modal h4').count() > 0;
    if (hasRecommendations) {
      await expect(page.locator('.dashboard-modal :has-text("accuracy"), .dashboard-modal :has-text("estimates")')).toBeVisible();
    }
  });
});
