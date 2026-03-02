/**
 * Automated screenshot generation for README and docs.
 *
 * Run:   npx playwright test tests/99-screenshots.spec.ts
 * Or:    npm run screenshots
 *
 * Output: ../docs/screenshots/*.png
 */
import { test, expect } from '@playwright/test';
import * as path from 'path';
import { SEL } from '../helpers/selectors';
import { BUCKETS } from '../helpers/test-data';
import {
  dismissWelcomeIfPresent,
  navigateToBucket,
} from '../helpers/wait-helpers';

const SCREENSHOT_DIR = path.resolve(__dirname, '..', '..', 'docs', 'screenshots');

const screenshot = (name: string) => ({
  path: path.join(SCREENSHOT_DIR, `${name}.png`),
  fullPage: false,
});

test.describe('Screenshots', () => {
  // Use a wider viewport for nicer screenshots
  test.use({
    viewport: { width: 1440, height: 900 },
  });

  test('00 — Login page', async ({ browser }) => {
    // Fresh context with no auth to show login page
    const context = await browser.newContext({
      viewport: { width: 1440, height: 900 },
      storageState: { cookies: [], origins: [] },
    });
    const page = await context.newPage();
    await page.goto('/');
    await expect(page.locator(SEL.loginForm)).toBeVisible({ timeout: 10_000 });
    await page.waitForTimeout(500);
    await page.screenshot(screenshot('login'));
    await context.close();
  });

  test('01 — Bucket list (light mode)', async ({ page }) => {
    await page.goto('/');
    await dismissWelcomeIfPresent(page);
    await expect(page.locator(SEL.bucketCard).first()).toBeVisible({ timeout: 15_000 });

    // Ensure light mode
    const theme = await page.evaluate(() => document.documentElement.getAttribute('data-theme'));
    if (theme === 'dark') {
      await page.locator(SEL.themeToggle).click();
      await page.waitForTimeout(300);
    }

    await page.screenshot(screenshot('bucket-list'));
  });

  test('02 — Object browser', async ({ page }) => {
    await page.goto('/');
    await dismissWelcomeIfPresent(page);

    // Ensure light mode
    const theme = await page.evaluate(() => document.documentElement.getAttribute('data-theme'));
    if (theme === 'dark') {
      await page.locator(SEL.themeToggle).click();
      await page.waitForTimeout(300);
    }

    await navigateToBucket(page, BUCKETS.MAIN);
    await expect(page.locator(SEL.tableRow).first()).toBeVisible();

    await page.screenshot(screenshot('object-browser'));
  });

  test('03 — Search results', async ({ page }) => {
    await page.goto('/');
    await dismissWelcomeIfPresent(page);

    // Ensure light mode
    const theme = await page.evaluate(() => document.documentElement.getAttribute('data-theme'));
    if (theme === 'dark') {
      await page.locator(SEL.themeToggle).click();
      await page.waitForTimeout(300);
    }

    await navigateToBucket(page, BUCKETS.MAIN);

    // Open search and type a query
    await page.locator(SEL.searchButton).click();
    await expect(page.locator(SEL.searchInput)).toBeVisible();
    await page.locator(SEL.searchInput).fill('sample');

    // Wait for search results to appear
    await expect(
      page.locator(`${SEL.searchItem}, ${SEL.searchCount}`).first()
    ).toBeVisible({ timeout: 10_000 });

    await page.screenshot(screenshot('search'));
  });

  test('04 — Storage dashboard', async ({ page }) => {
    await page.goto('/');
    await dismissWelcomeIfPresent(page);

    // Ensure light mode
    const theme = await page.evaluate(() => document.documentElement.getAttribute('data-theme'));
    if (theme === 'dark') {
      await page.locator(SEL.themeToggle).click();
      await page.waitForTimeout(300);
    }

    await navigateToBucket(page, BUCKETS.MAIN);

    // Open storage dashboard
    await page.locator(SEL.dashboardButton).click();
    await expect(page.locator(SEL.dashboardModal)).toBeVisible();
    await expect(page.locator(SEL.dashboardCard).first()).toBeVisible({ timeout: 10_000 });

    // Wait for bar chart to render
    await expect(page.locator(SEL.dashboardBarRow).first()).toBeVisible({ timeout: 10_000 });

    await page.screenshot(screenshot('storage-dashboard'));
  });

  test('05 — Object details', async ({ page }) => {
    await page.goto('/');
    await dismissWelcomeIfPresent(page);

    // Ensure light mode
    const theme = await page.evaluate(() => document.documentElement.getAttribute('data-theme'));
    if (theme === 'dark') {
      await page.locator(SEL.themeToggle).click();
      await page.waitForTimeout(300);
    }

    await navigateToBucket(page, BUCKETS.MAIN);

    // Click the info button on sample.json
    const fileRow = page.locator(`${SEL.tableRow}:has-text("sample.json")`);
    await expect(fileRow).toBeVisible();
    await fileRow.locator(`${SEL.colActions} button:has-text("i")`).click();
    await expect(page.locator(SEL.modal)).toBeVisible();

    // Wait for details to load
    await expect(page.locator(SEL.infoTable)).toBeVisible({ timeout: 10_000 });

    await page.screenshot(screenshot('file-details'));
  });

  test('06 — Dark mode', async ({ page }) => {
    await page.goto('/');
    await dismissWelcomeIfPresent(page);
    await expect(page.locator(SEL.bucketCard).first()).toBeVisible({ timeout: 15_000 });

    // Switch to dark mode
    const theme = await page.evaluate(() => document.documentElement.getAttribute('data-theme'));
    if (theme !== 'dark') {
      await page.locator(SEL.themeToggle).click();
      await page.waitForTimeout(300);
    }

    await page.screenshot(screenshot('dark-mode'));
  });

  test('07 — Object browser (dark mode)', async ({ page }) => {
    await page.goto('/');
    await dismissWelcomeIfPresent(page);

    // Switch to dark mode
    const theme = await page.evaluate(() => document.documentElement.getAttribute('data-theme'));
    if (theme !== 'dark') {
      await page.locator(SEL.themeToggle).click();
      await page.waitForTimeout(300);
    }

    await navigateToBucket(page, BUCKETS.MAIN);
    await expect(page.locator(SEL.tableRow).first()).toBeVisible();

    await page.screenshot(screenshot('object-browser-dark'));
  });

  test('08 — Search results (dark mode)', async ({ page }) => {
    await page.goto('/');
    await dismissWelcomeIfPresent(page);

    const theme = await page.evaluate(() => document.documentElement.getAttribute('data-theme'));
    if (theme !== 'dark') {
      await page.locator(SEL.themeToggle).click();
      await page.waitForTimeout(300);
    }

    await navigateToBucket(page, BUCKETS.MAIN);
    await page.locator(SEL.searchButton).click();
    await expect(page.locator(SEL.searchInput)).toBeVisible();
    await page.locator(SEL.searchInput).fill('sample');
    await expect(
      page.locator(`${SEL.searchItem}, ${SEL.searchCount}`).first()
    ).toBeVisible({ timeout: 10_000 });

    await page.screenshot(screenshot('search-dark'));
  });

  test('09 — Storage dashboard (dark mode)', async ({ page }) => {
    await page.goto('/');
    await dismissWelcomeIfPresent(page);

    const theme = await page.evaluate(() => document.documentElement.getAttribute('data-theme'));
    if (theme !== 'dark') {
      await page.locator(SEL.themeToggle).click();
      await page.waitForTimeout(300);
    }

    await navigateToBucket(page, BUCKETS.MAIN);
    await page.locator(SEL.dashboardButton).click();
    await expect(page.locator(SEL.dashboardModal)).toBeVisible();
    await expect(page.locator(SEL.dashboardCard).first()).toBeVisible({ timeout: 10_000 });
    await expect(page.locator(SEL.dashboardBarRow).first()).toBeVisible({ timeout: 10_000 });

    await page.screenshot(screenshot('storage-dashboard-dark'));
  });

  test('10 — Object details (dark mode)', async ({ page }) => {
    await page.goto('/');
    await dismissWelcomeIfPresent(page);

    const theme = await page.evaluate(() => document.documentElement.getAttribute('data-theme'));
    if (theme !== 'dark') {
      await page.locator(SEL.themeToggle).click();
      await page.waitForTimeout(300);
    }

    await navigateToBucket(page, BUCKETS.MAIN);

    const fileRow = page.locator(`${SEL.tableRow}:has-text("sample.json")`);
    await expect(fileRow).toBeVisible();
    await fileRow.locator(`${SEL.colActions} button:has-text("i")`).click();
    await expect(page.locator(SEL.modal)).toBeVisible();
    await expect(page.locator(SEL.infoTable)).toBeVisible({ timeout: 10_000 });

    await page.screenshot(screenshot('file-details-dark'));
  });
});
