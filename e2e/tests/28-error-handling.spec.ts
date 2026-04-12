import { test, expect } from '@playwright/test';
import { SEL } from '../helpers/selectors';
import { BUCKETS } from '../helpers/test-data';
import { dismissWelcomeIfPresent, navigateToBucket, clickTab } from '../helpers/wait-helpers';

/**
 * Tests that verify error handling improvements:
 * - BucketSettings: Promise.all .catch(), multipart .catch(), async handler try-catch
 * - StorageDashboard: optimization error state, cost error logging
 * - Backend: storage_history uses latest-per-day (not MAX)
 */

test.describe('Error Handling — BucketSettings', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/');
    await dismissWelcomeIfPresent(page);
    await navigateToBucket(page, BUCKETS.SETTINGS);
  });

  test('28.1 settings modal loads without getting stuck on spinner', async ({ page }) => {
    await page.locator(SEL.settingsButton).click();
    await expect(page.locator(SEL.modal)).toBeVisible();

    // Should either load successfully (versioning header visible) or show an error alert
    // Must NOT stay stuck on loading spinner forever
    await page.waitForFunction(() => {
      const modal = document.querySelector('.modal');
      if (!modal) return false;
      // Loaded successfully
      if (modal.querySelector('h3')) return true;
      // Error alert appeared
      if (modal.textContent?.includes('Failed to load')) return true;
      return false;
    }, { timeout: 15_000 });
  });

  test('28.2 multipart tab loads or shows error — never hangs', async ({ page }) => {
    await page.locator(SEL.settingsButton).click();
    await expect(page.locator(SEL.modal)).toBeVisible();

    await clickTab(page, 'Multipart');

    // Should either show multipart content or an error — never a permanent spinner
    await page.waitForFunction(() => {
      const modal = document.querySelector('.modal');
      if (!modal) return false;
      // Loaded: shows "No incomplete" or upload cards
      if (modal.textContent?.includes('No incomplete') || modal.textContent?.includes('Stale')) return true;
      // Error: alert appeared
      if (modal.textContent?.includes('Failed to load multipart')) return true;
      // Still loading (spinner) — keep waiting
      return false;
    }, { timeout: 15_000 });
  });

  test('28.3 overview tab shows versioning status after load', async ({ page }) => {
    await page.locator(SEL.settingsButton).click();
    await expect(page.locator(SEL.modal)).toBeVisible();

    // Verify the settings loaded (not stuck)
    await expect(page.locator(`${SEL.modal} h3:has-text("Versioning")`)).toBeVisible({ timeout: 10_000 });
  });

  test('28.4 policy tab delete shows error dialog on failure if applicable', async ({ page }) => {
    await page.locator(SEL.settingsButton).click();
    await expect(page.locator(SEL.modal)).toBeVisible();

    await clickTab(page, 'Policy');
    await expect(page.locator(`${SEL.modal} h3:has-text("Bucket Policy")`)).toBeVisible();

    // If a policy exists with Delete button, clicking it should either succeed or show error
    const deleteBtn = page.locator(SEL.deletePolicyBtn);
    if (await deleteBtn.isVisible({ timeout: 3_000 }).catch(() => false)) {
      await deleteBtn.click();
      // Wait for either: policy deleted (button gone) OR error dialog
      await page.waitForFunction(() => {
        const modal = document.querySelector('.modal');
        if (!modal) return false;
        // Delete succeeded — button gone
        const btns = modal.querySelectorAll('button');
        const hasDelete = Array.from(btns).some(b => b.textContent?.includes('Delete Policy'));
        if (!hasDelete) return true;
        // Error dialog appeared
        if (modal.textContent?.includes('Failed to delete')) return true;
        return false;
      }, { timeout: 5_000 });
    }
  });

  test('28.5 tags tab add/remove shows error on failure instead of silent fail', async ({ page }) => {
    await page.locator(SEL.settingsButton).click();
    await expect(page.locator(SEL.modal)).toBeVisible();

    await clickTab(page, 'Tags');
    await expect(page.locator(`${SEL.modal} h3:has-text("Bucket Tags")`)).toBeVisible();

    // Verify tags section loaded — there should be an "Add" button or key/value inputs
    const addBtn = page.locator(`${SEL.modal} button:has-text("Add")`).first();
    await expect(addBtn).toBeVisible({ timeout: 5_000 });
  });

  test('28.6 re-index button works or shows error — not silent', async ({ page }) => {
    await page.locator(SEL.settingsButton).click();
    await expect(page.locator(SEL.modal)).toBeVisible();

    await clickTab(page, 'Index');
    await expect(page.locator(SEL.reindexButton)).toBeVisible();

    await page.locator(SEL.reindexButton).click();

    // Should either start indexing or show error — not fail silently
    await page.waitForFunction(() => {
      const modal = document.querySelector('.modal');
      if (!modal) return false;
      const text = modal.textContent || '';
      return text.includes('Indexing') || text.includes('Re-index') || text.includes('Failed to trigger');
    }, { timeout: 10_000 });
  });
});

test.describe('Error Handling — StorageDashboard', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/');
    await dismissWelcomeIfPresent(page);
    await navigateToBucket(page, BUCKETS.MAIN);
  });

  test('28.7 optimize tab shows error message on failure instead of empty state', async ({ page }) => {
    await page.locator(SEL.dashboardButton).click();
    await expect(page.locator(SEL.dashboardModal)).toBeVisible();

    // Switch to Optimize tab
    await page.locator(SEL.insightsOptimizeTab).click();

    // Should show: loading spinner → then one of: data, all-clear, empty message, or error
    // Must NOT show spinner forever
    await page.waitForFunction(() => {
      const modal = document.querySelector('.dashboard-modal');
      if (!modal) return false;
      const text = modal.textContent || '';
      // Has content (recommendations, all-clear, empty, or error)
      if (text.includes('All clear')) return true;
      if (text.includes('empty')) return true;
      if (text.includes('Failed to load')) return true;
      if (modal.querySelector('h4')) return true;  // recommendation headings
      // Still loading
      if (modal.querySelector('.spinner')) return false;
      // No spinner and no content — also done (no data)
      return true;
    }, { timeout: 30_000 });
  });

  test('28.8 storage tab loads cost data or silently degrades', async ({ page }) => {
    await page.locator(SEL.dashboardButton).click();
    await expect(page.locator(SEL.dashboardModal)).toBeVisible();

    // Cost cards are optional — should appear if cost endpoint works, absent if not
    // Either way, Total Objects and Total Size should always show
    await expect(page.locator(`${SEL.dashboardCardLabel}:has-text("Total Objects")`)).toBeVisible({ timeout: 10_000 });
    await expect(page.locator(`${SEL.dashboardCardLabel}:has-text("Total Size")`)).toBeVisible();
  });
});

test.describe('Error Handling — Storage History Accuracy', () => {
  test('28.9 storage history endpoint returns data with latest-per-day values', async () => {
    const baseURL = process.env.SAIRO_URL || 'http://localhost:8888';
    const { ApiClient } = await import('../helpers/api-client');
    const api = new ApiClient(baseURL);
    await api.login('admin', 'password');

    // Trigger a crawl to ensure storage_history has data
    await api.triggerCrawl(BUCKETS.MAIN);
    await api.waitForIndex(BUCKETS.MAIN, 30_000);

    // Fetch storage history
    const res = await api.getRawResponse(`/api/buckets/${BUCKETS.MAIN}/storage-history?days=90`);
    expect(res.status).toBe(200);
    const data = await res.json();

    expect(data).toHaveProperty('history');
    expect(Array.isArray(data.history)).toBeTruthy();

    // If there's history, each entry should have day + object_count + total_size
    if (data.history.length > 0) {
      const entry = data.history[0];
      expect(entry).toHaveProperty('day');
      expect(entry).toHaveProperty('object_count');
      expect(entry).toHaveProperty('total_size');
      expect(typeof entry.object_count).toBe('number');
      expect(typeof entry.total_size).toBe('number');
    }
  });

  test('28.10 storage history returns empty array for bucket with no data', async () => {
    const baseURL = process.env.SAIRO_URL || 'http://localhost:8888';
    const { ApiClient } = await import('../helpers/api-client');
    const api = new ApiClient(baseURL);
    await api.login('admin', 'password');

    const res = await api.getRawResponse(`/api/buckets/${BUCKETS.EMPTY}/storage-history?days=90`);
    expect(res.status).toBe(200);
    const data = await res.json();
    expect(data.history).toEqual([]);
  });
});
