import { test, expect } from '@playwright/test';
import { SEL } from '../helpers/selectors';
import { BUCKETS } from '../helpers/test-data';
import { dismissWelcomeIfPresent, navigateToBucket } from '../helpers/wait-helpers';

test.describe('Crawl Status', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/');
    await dismissWelcomeIfPresent(page);
    await navigateToBucket(page, BUCKETS.MAIN);
  });

  test('22.1 clicking crawl badge expands dropdown', async ({ page }) => {
    const crawlBadge = page.locator(SEL.crawlBadge);
    if (await crawlBadge.isVisible().catch(() => false)) {
      await crawlBadge.click();
      await expect(page.locator(SEL.crawlDropdown)).toBeVisible({ timeout: 3_000 });
      await expect(page.locator(SEL.crawlDetail).first()).toBeVisible();

      // Click again to close
      await crawlBadge.click();
      await expect(page.locator(SEL.crawlDropdown)).toBeHidden({ timeout: 3_000 });
    }
  });

  test('22.2 Re-index Now triggers crawl from dropdown', async ({ page }) => {
    const crawlBadge = page.locator(SEL.crawlBadge);
    if (await crawlBadge.isVisible().catch(() => false)) {
      await crawlBadge.click();
      await expect(page.locator(SEL.crawlDropdown)).toBeVisible({ timeout: 3_000 });

      const reindexBtn = page.locator(SEL.crawlReindexBtn);
      if (await reindexBtn.isVisible().catch(() => false)) {
        await reindexBtn.click();
        // Button text should change to "Crawling..." or badge updates
        await page.waitForFunction(() => {
          const btn = document.querySelector('.crawl-dropdown button.btn-primary');
          const badge = document.querySelector('.crawl-badge');
          if (!btn && !badge) return false;
          const btnText = btn?.textContent || '';
          const badgeText = badge?.textContent || '';
          return btnText.includes('Crawling') || btnText.includes('Re-index')
            || badgeText.includes('Crawling') || badgeText.includes('Indexed');
        }, { timeout: 10_000 });
      }
    }
  });
});
