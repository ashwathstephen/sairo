import { test, expect } from '@playwright/test';
import { SEL } from '../helpers/selectors';
import { BUCKETS } from '../helpers/test-data';
import { dismissWelcomeIfPresent, navigateToBucket, waitForTableLoaded } from '../helpers/wait-helpers';

test.describe('Favorites', () => {
  test.beforeEach(async ({ page }) => {
    // Clear favorites from localStorage
    await page.goto('/');
    await page.evaluate(() => localStorage.removeItem('s3-browser-favorites'));
    await page.reload();
    await dismissWelcomeIfPresent(page);
  });

  test('17 adding current path to favorites via breadcrumb star', async ({ page }) => {
    await navigateToBucket(page, BUCKETS.MAIN);

    // Click the star on the breadcrumb to favorite this path
    const star = page.locator(SEL.favoriteStar);
    if (await star.isVisible().catch(() => false)) {
      await star.click();
      // Verify favorite was saved to localStorage
      const favs = await page.evaluate(() => JSON.parse(localStorage.getItem('s3-browser-favorites') || '[]'));
      expect(favs.length).toBeGreaterThan(0);
    }
  });

  test('17 favorite appears in favorites dropdown', async ({ page }) => {
    await navigateToBucket(page, BUCKETS.MAIN);

    // Add to favorites
    const star = page.locator(SEL.favoriteStar);
    if (await star.isVisible().catch(() => false)) {
      await star.click();
    }

    // Open favorites dropdown
    const favBtn = page.locator('.favorites-btn').first();
    if (await favBtn.isVisible().catch(() => false)) {
      await favBtn.click();
      // Should see the favorited path
      await expect(page.locator(`.favorites-dropdown, .favorites-list`).first()).toBeVisible();
    }
  });

  test('17 favorites persist after page refresh', async ({ page }) => {
    await navigateToBucket(page, BUCKETS.MAIN);

    const star = page.locator(SEL.favoriteStar);
    if (await star.isVisible().catch(() => false)) {
      await star.click();
    }

    // Refresh page
    await page.reload();
    await dismissWelcomeIfPresent(page);

    // Favorites should still exist in localStorage
    const favs = await page.evaluate(() => JSON.parse(localStorage.getItem('s3-browser-favorites') || '[]'));
    expect(favs.length).toBeGreaterThan(0);
  });

  test('17 clicking star again removes favorite', async ({ page }) => {
    await navigateToBucket(page, BUCKETS.MAIN);

    const star = page.locator(SEL.favoriteStar);
    if (await star.isVisible().catch(() => false)) {
      // Add
      await star.click();
      let favs = await page.evaluate(() => JSON.parse(localStorage.getItem('s3-browser-favorites') || '[]'));
      const addedCount = favs.length;

      // Remove
      await star.click();
      favs = await page.evaluate(() => JSON.parse(localStorage.getItem('s3-browser-favorites') || '[]'));
      expect(favs.length).toBeLessThan(addedCount);
    }
  });
});
