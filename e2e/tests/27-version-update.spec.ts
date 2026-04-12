import { test, expect } from '@playwright/test';
import { ApiClient } from '../helpers/api-client';
import { ADMIN, BUCKETS } from '../helpers/test-data';
import { SEL } from '../helpers/selectors';
import { dismissWelcomeIfPresent, navigateToBucket } from '../helpers/wait-helpers';

test.describe('Version Check & Update Banner', () => {
  const baseURL = process.env.SAIRO_URL || 'http://localhost:8888';

  test('27.1 /api/version endpoint returns version info', async () => {
    const api = new ApiClient(baseURL);
    await api.login(ADMIN.username, ADMIN.password);
    const data = await api.checkForUpdate();
    // Should return an object with version info (or null if GitHub unreachable)
    if (data) {
      expect(data).toHaveProperty('current_version');
    }
  });

  test('27.2 /api/pricing endpoint returns provider pricing', async () => {
    const api = new ApiClient(baseURL);
    await api.login(ADMIN.username, ADMIN.password);
    const data = await api.getPricing();
    expect(data).toBeDefined();
    // Should have pricing data for multiple providers
    if (data.providers) {
      expect(data.providers.length).toBeGreaterThan(0);
    }
  });

  test('27.3 optimization summary endpoint works for indexed bucket', async () => {
    const api = new ApiClient(baseURL);
    await api.login(ADMIN.username, ADMIN.password);

    // Trigger crawl first to ensure data exists
    await api.triggerCrawl(BUCKETS.MAIN);
    await api.waitForIndex(BUCKETS.MAIN, 30_000);

    const data = await api.getOptimizationSummary(BUCKETS.MAIN);
    expect(data).toBeDefined();
    // Should have core optimization fields
    expect(data).toHaveProperty('total_objects');
    expect(data).toHaveProperty('total_size');
  });

  test('27.4 cost breakdown endpoint works', async () => {
    const api = new ApiClient(baseURL);
    await api.login(ADMIN.username, ADMIN.password);
    const data = await api.getCostBreakdown(BUCKETS.MAIN);
    expect(data).toBeDefined();
  });

  test('27.5 multipart uploads endpoint works', async () => {
    const api = new ApiClient(baseURL);
    await api.login(ADMIN.username, ADMIN.password);
    const data = await api.getMultipartUploads(BUCKETS.MAIN);
    expect(data).toBeDefined();
    // Should have count field
    expect(data).toHaveProperty('count');
    expect(typeof data.count).toBe('number');
  });

  test('27.6 multipart uploads with details flag works', async () => {
    const api = new ApiClient(baseURL);
    await api.login(ADMIN.username, ADMIN.password);
    const data = await api.getMultipartUploads(BUCKETS.MAIN, true);
    expect(data).toBeDefined();
    expect(data).toHaveProperty('count');
    // When details=true, uploads array should have part info if any exist
    if (data.uploads && data.uploads.length > 0) {
      expect(data.uploads[0]).toHaveProperty('key');
    }
  });

  test('27.7 Insights button visible in bucket view', async ({ page }) => {
    await page.goto('/');
    await dismissWelcomeIfPresent(page);
    await navigateToBucket(page, BUCKETS.MAIN);

    // The button says "Insights" but has aria-label "Storage Dashboard"
    const insightsBtn = page.locator(SEL.dashboardButton);
    await expect(insightsBtn).toBeVisible();
    await expect(insightsBtn).toContainText('Insights');
  });
});
