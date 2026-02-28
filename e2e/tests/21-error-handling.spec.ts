import { test, expect } from '@playwright/test';
import { SEL } from '../helpers/selectors';
import { BUCKETS } from '../helpers/test-data';
import { dismissWelcomeIfPresent, navigateToBucket, waitForTableLoaded } from '../helpers/wait-helpers';

test.describe('Error Handling & Edge Cases', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/');
    await dismissWelcomeIfPresent(page);
  });

  test('21 empty bucket shows "No objects" empty state', async ({ page }) => {
    await navigateToBucket(page, BUCKETS.EMPTY);
    await expect(page.locator(SEL.emptyState)).toBeVisible({ timeout: 10_000 });
  });

  test('21 filter with no matches shows empty state', async ({ page }) => {
    await navigateToBucket(page, BUCKETS.MAIN);

    await page.locator(SEL.filterInput).fill('zzz-absolutely-no-match-xyz');

    // Should show no rows (or an empty state)
    await page.waitForTimeout(500);
    const visibleRows = page.locator(`${SEL.tableRow}:visible`);
    const count = await visibleRows.count();
    expect(count).toBe(0);
  });

  test('21 special characters in folder names handled correctly', async ({ page }) => {
    await navigateToBucket(page, BUCKETS.MAIN);

    // Create a folder with special characters
    await page.locator(SEL.newFolderButton).click();
    await expect(page.locator(SEL.modal)).toBeVisible();
    await page.locator(SEL.promptInput).fill('test-special_chars.2024');
    await page.locator(SEL.promptSubmit).click();

    // Modal should close after successful creation
    await expect(page.locator(SEL.modal)).toBeHidden({ timeout: 10_000 });

    // Page should still show object listing (not crash)
    await expect(page.locator(SEL.tableRow).first().or(page.locator(SEL.emptyState))).toBeVisible({ timeout: 10_000 });
  });

  test('21 navigating to non-existent bucket shows warning', async ({ page }) => {
    // Navigate directly via hash to a non-existent bucket
    await page.goto('/#nonexistent-bucket-xyz-12345');

    // Should show warning toast and/or redirect to bucket list
    await page.waitForTimeout(3000);

    // Either on bucket list or showing an error toast
    const hasBucketList = await page.locator(SEL.bucketCard).first().isVisible().catch(() => false);
    const hasToast = await page.locator(SEL.toast).first().isVisible().catch(() => false);
    expect(hasBucketList || hasToast).toBe(true);
  });
});
