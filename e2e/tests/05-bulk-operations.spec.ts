import { test, expect } from '@playwright/test';
import { SEL } from '../helpers/selectors';
import { BUCKETS } from '../helpers/test-data';
import { dismissWelcomeIfPresent, navigateToBucket, waitForToast, waitForTableLoaded } from '../helpers/wait-helpers';

test.describe('Bulk Operations (Copy/Move)', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/');
    await dismissWelcomeIfPresent(page);
    await navigateToBucket(page, BUCKETS.MAIN);
  });

  test('5.1 shows bulk action bar when files are selected', async ({ page }) => {
    // Select first file
    const firstFile = page.locator(`${SEL.tableRow}:not(.row-folder) input[type="checkbox"]`).first();
    if (await firstFile.isVisible().catch(() => false)) {
      await firstFile.check();
      await expect(page.locator(SEL.bulkBar)).toBeVisible();
      await expect(page.locator(SEL.bulkBarCount)).toContainText('selected');
    }
  });

  test('5.1 bulk bar has Copy, Move, and Delete buttons', async ({ page }) => {
    const firstFile = page.locator(`${SEL.tableRow}:not(.row-folder) input[type="checkbox"]`).first();
    if (await firstFile.isVisible().catch(() => false)) {
      await firstFile.check();
      await expect(page.locator(SEL.bulkCopyBtn)).toBeVisible();
      await expect(page.locator(SEL.bulkMoveBtn)).toBeVisible();
      await expect(page.locator(SEL.bulkDeleteBtn)).toBeVisible();
    }
  });

  test('5.2 opens FolderPicker on Copy to click', async ({ page }) => {
    const firstFile = page.locator(`${SEL.tableRow}:not(.row-folder) input[type="checkbox"]`).first();
    if (await firstFile.isVisible().catch(() => false)) {
      await firstFile.check();
      await page.locator(SEL.bulkCopyBtn).click();

      // FolderPicker modal should appear
      await expect(page.locator(SEL.modal)).toBeVisible();
      // Should have a bucket selector
      await expect(page.locator(`${SEL.modal} select`).first()).toBeVisible();

      // Close without copying
      await page.locator(`${SEL.modal} button:has-text("Cancel")`).click();
    }
  });

  test('5.2 copies files to destination bucket', async ({ page }) => {
    const firstFile = page.locator(`${SEL.tableRow}:not(.row-folder) input[type="checkbox"]`).first();
    if (await firstFile.isVisible().catch(() => false)) {
      await firstFile.check();
      await page.locator(SEL.bulkCopyBtn).click();

      // Select destination bucket
      const bucketSelect = page.locator(`${SEL.modal} select`).first();
      await bucketSelect.selectOption(BUCKETS.COPY_DEST);

      // Click "Copy here"
      const copyHere = page.locator(`${SEL.modal} button.btn-primary`);
      await copyHere.click();

      // Wait for completion toast
      await waitForToast(page, 'Copied');
    }
  });

  test('5.3 opens FolderPicker on Move to click', async ({ page }) => {
    const firstFile = page.locator(`${SEL.tableRow}:not(.row-folder) input[type="checkbox"]`).first();
    if (await firstFile.isVisible().catch(() => false)) {
      await firstFile.check();
      await page.locator(SEL.bulkMoveBtn).click();

      await expect(page.locator(SEL.modal)).toBeVisible();
      // Cancel without moving
      await page.locator(`${SEL.modal} button:has-text("Cancel")`).click();
    }
  });
});
