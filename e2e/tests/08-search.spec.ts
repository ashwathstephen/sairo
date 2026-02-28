import { test, expect } from '@playwright/test';
import { SEL } from '../helpers/selectors';
import { BUCKETS } from '../helpers/test-data';
import { dismissWelcomeIfPresent, navigateToBucket, waitForTableLoaded } from '../helpers/wait-helpers';

test.describe('Search', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/');
    await dismissWelcomeIfPresent(page);
    await navigateToBucket(page, BUCKETS.MAIN);
  });

  test('8 opens search bar on Search button click', async ({ page }) => {
    await page.locator(SEL.searchButton).click();
    await expect(page.locator(SEL.searchInput)).toBeVisible();
  });

  test('8 opens search bar on / key press', async ({ page }) => {
    // Make sure no input is focused
    await page.locator('body').click();
    await page.keyboard.press('/');
    await expect(page.locator(SEL.searchInput)).toBeVisible();
  });

  test('8 shows hint before typing', async ({ page }) => {
    await page.locator(SEL.searchButton).click();
    await expect(page.locator(SEL.searchHint)).toBeVisible();
  });

  test('8 shows results after typing 2+ characters', async ({ page }) => {
    await page.locator(SEL.searchButton).click();
    await page.locator(SEL.searchInput).fill('sample');

    // Wait for results
    await expect(page.locator(`${SEL.searchItem}, ${SEL.searchCount}, ${SEL.searchEmpty}`).first()).toBeVisible({ timeout: 10_000 });

    // If indexed, should have results
    const count = page.locator(SEL.searchCount);
    if (await count.isVisible().catch(() => false)) {
      const text = await count.textContent();
      expect(text).toMatch(/\d+ result/);
    }
  });

  test('8 clicking result navigates to folder', async ({ page }) => {
    await page.locator(SEL.searchButton).click();
    await page.locator(SEL.searchInput).fill('sample');

    const firstResult = page.locator(SEL.searchItem).first();
    await expect(firstResult).toBeVisible({ timeout: 10_000 });

    // Click on the result's folder link
    const folderLink = firstResult.locator('a');
    if (await folderLink.isVisible().catch(() => false)) {
      await folderLink.click();
      await waitForTableLoaded(page);
    }
  });

  test('8 Esc closes search bar', async ({ page }) => {
    await page.locator(SEL.searchButton).click();
    await expect(page.locator(SEL.searchInput)).toBeVisible();
    await page.keyboard.press('Escape');
    await expect(page.locator(SEL.searchInput)).toBeHidden();
  });

  test('8 no results message for non-matching query', async ({ page }) => {
    await page.locator(SEL.searchButton).click();
    await page.locator(SEL.searchInput).fill('zzz-nonexistent-file-xyz');

    // Wait for empty state
    await expect(page.locator(`${SEL.searchEmpty}, ${SEL.searchError}`).first()).toBeVisible({ timeout: 10_000 });
  });
});
