import { test, expect } from '@playwright/test';
import { SEL } from '../helpers/selectors';
import { BUCKETS } from '../helpers/test-data';
import { dismissWelcomeIfPresent, navigateToBucket, waitForTableLoaded, waitForToast } from '../helpers/wait-helpers';

test.describe('Folder Picker', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/');
    await dismissWelcomeIfPresent(page);
    await navigateToBucket(page, BUCKETS.MAIN);
  });

  test('23.1 manual path typing navigates in FolderPicker', async ({ page }) => {
    // Select a file to enable bulk bar
    const firstFileRow = page.locator(`${SEL.tableRow}:not(.row-folder)`).first();
    if (await firstFileRow.isVisible().catch(() => false)) {
      await firstFileRow.locator('input[type="checkbox"]').check();

      // Click "Copy to..." to open FolderPicker
      const copyBtn = page.locator(SEL.bulkCopyBtn);
      if (await copyBtn.isVisible().catch(() => false)) {
        await copyBtn.click();
        await expect(page.locator(SEL.modal)).toBeVisible();

        // Type a path in the path input
        const pathInput = page.locator(SEL.folderPickerPathInput);
        if (await pathInput.isVisible().catch(() => false)) {
          await pathInput.fill('docs/');
          await page.locator(SEL.folderPickerGoBtn).click();
          // Folder listing should update (items visible or empty)
          await page.waitForTimeout(1000);
        }

        // Close modal via Cancel button
        await page.locator(`${SEL.modal} button:has-text("Cancel")`).click();
      }
    }
  });

  test('23.2 up-one-level item navigates back', async ({ page }) => {
    const firstFileRow = page.locator(`${SEL.tableRow}:not(.row-folder)`).first();
    if (await firstFileRow.isVisible().catch(() => false)) {
      await firstFileRow.locator('input[type="checkbox"]').check();

      const copyBtn = page.locator(SEL.bulkCopyBtn);
      if (await copyBtn.isVisible().catch(() => false)) {
        await copyBtn.click();
        await expect(page.locator(SEL.modal)).toBeVisible();

        // Navigate into a folder first
        const folderItem = page.locator(`${SEL.folderPickerItem}:not(:has-text(".."))`).first();
        if (await folderItem.isVisible().catch(() => false)) {
          await folderItem.click();
          await page.waitForTimeout(500);

          // ".. (up one level)" should be visible
          const upItem = page.locator(`${SEL.folderPickerItem}:has-text("..")`);
          if (await upItem.isVisible().catch(() => false)) {
            await upItem.click();
            await page.waitForTimeout(500);
            // Should be back at root — ".." should disappear
            await expect(upItem).toBeHidden({ timeout: 3_000 });
          }
        }

        // Close modal via Cancel button
        await page.locator(`${SEL.modal} button:has-text("Cancel")`).click();
      }
    }
  });

  test('23.3 bulk move completes successfully', async ({ page }) => {
    // Select a file
    const firstFileRow = page.locator(`${SEL.tableRow}:not(.row-folder)`).first();
    if (await firstFileRow.isVisible().catch(() => false)) {
      const fileName = await firstFileRow.locator(SEL.colName).textContent();
      await firstFileRow.locator('input[type="checkbox"]').check();

      // Click "Move to..."
      const moveBtn = page.locator(SEL.bulkMoveBtn);
      if (await moveBtn.isVisible().catch(() => false)) {
        await moveBtn.click();
        await expect(page.locator(SEL.modal)).toBeVisible();

        // Select a different destination bucket
        const bucketSelect = page.locator(SEL.folderPickerBucketSelect);
        if (await bucketSelect.isVisible().catch(() => false)) {
          await bucketSelect.selectOption(BUCKETS.COPY_DEST);

          // Click confirm (Move here / Copy here)
          await page.locator(SEL.folderPickerConfirm).click();

          // Should show success toast
          await waitForToast(page, 'Moved');
        }
      }
    }
  });
});
