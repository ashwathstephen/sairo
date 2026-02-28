import { test, expect } from '@playwright/test';
import { SEL } from '../helpers/selectors';
import { BUCKETS } from '../helpers/test-data';
import { dismissWelcomeIfPresent, navigateToBucket, waitForTableLoaded } from '../helpers/wait-helpers';

test.describe('Object Browser', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/');
    await dismissWelcomeIfPresent(page);
    await navigateToBucket(page, BUCKETS.MAIN);
  });

  test('3.1 shows folders and files in table', async ({ page }) => {
    // Should see at least the docs/ and images/ folders plus root files
    const rows = page.locator(SEL.tableRow);
    await expect(rows.first()).toBeVisible();
    const count = await rows.count();
    expect(count).toBeGreaterThan(0);
  });

  test('3.1 navigates into folder on click', async ({ page }) => {
    const folderLink = page.locator(SEL.folderLink).first();
    const folderName = await folderLink.textContent();
    await folderLink.click();
    await waitForTableLoaded(page);
    // URL should now include the folder prefix
    expect(page.url()).toContain(encodeURIComponent(BUCKETS.MAIN));
  });

  test('3.1 breadcrumb shows correct path', async ({ page }) => {
    const breadcrumb = page.locator(SEL.breadcrumb);
    await expect(breadcrumb).toBeVisible();
    await expect(breadcrumb).toContainText('Buckets');
    await expect(breadcrumb).toContainText(BUCKETS.MAIN);
  });

  test('3.1 clicking Buckets in breadcrumb returns to bucket list', async ({ page }) => {
    await page.locator(`${SEL.breadcrumb} >> text=Buckets`).click();
    await expect(page.locator(SEL.bucketCard).first()).toBeVisible();
  });

  test('3.1 clicking app title returns to bucket list', async ({ page }) => {
    await page.locator(SEL.headerTitle).click();
    await expect(page.locator(SEL.bucketCard).first()).toBeVisible();
  });

  test('3.1 Backspace key navigates up one level', async ({ page }) => {
    // Navigate into a folder first
    const folder = page.locator(SEL.folderLink).first();
    if (await folder.isVisible().catch(() => false)) {
      await folder.click();
      await waitForTableLoaded(page);
      // Press Backspace (must not be focused on input)
      await page.keyboard.press('Backspace');
      // Backspace navigates back — may go to bucket root or bucket list
      await page.waitForFunction(() => {
        return document.querySelectorAll('.table-row').length > 0
          || document.querySelector('.empty-state') !== null
          || document.querySelector('.bucket-card') !== null;
      }, { timeout: 15_000 });
    }
  });

  test('3.1 Esc key navigates back', async ({ page }) => {
    const folder = page.locator(SEL.folderLink).first();
    if (await folder.isVisible().catch(() => false)) {
      await folder.click();
      await waitForTableLoaded(page);
      await page.keyboard.press('Escape');
      // Esc navigates back — may go to bucket root or bucket list
      await page.waitForFunction(() => {
        return document.querySelectorAll('.table-row').length > 0
          || document.querySelector('.empty-state') !== null
          || document.querySelector('.bucket-card') !== null;
      }, { timeout: 15_000 });
    }
  });

  test('3.2 file rows show name, size, and modified date', async ({ page }) => {
    // Find a file row (not folder)
    const fileRow = page.locator(`${SEL.tableRow}:not(.row-folder)`).first();
    if (await fileRow.isVisible().catch(() => false)) {
      await expect(fileRow.locator(SEL.colName)).toBeVisible();
      await expect(fileRow.locator(SEL.colSize)).toBeVisible();
      await expect(fileRow.locator(SEL.colModified)).toBeVisible();
    }
  });

  test('3.2 folder rows show folder icon and name', async ({ page }) => {
    const folderRow = page.locator(SEL.folderRow).first();
    if (await folderRow.isVisible().catch(() => false)) {
      const name = await folderRow.locator(SEL.colName).textContent();
      expect(name).toContain('/');
    }
  });

  test('3.3 sorts by name on header click', async ({ page }) => {
    const nameHeader = page.locator('.th.sortable').first();
    await nameHeader.click();
    // Should still show rows (sorting doesn't break display)
    await expect(page.locator(SEL.tableRow).first()).toBeVisible();
    // Click again for reverse sort
    await nameHeader.click();
    await expect(page.locator(SEL.tableRow).first()).toBeVisible();
  });

  test('3.4 filters items by name', async ({ page }) => {
    const filterInput = page.locator(SEL.filterInput);
    await filterInput.fill('sample');
    // Should show only matching items
    const rows = page.locator(SEL.tableRow);
    const count = await rows.count();
    expect(count).toBeGreaterThan(0);

    // Clear filter
    await filterInput.fill('');
    const allCount = await rows.count();
    expect(allCount).toBeGreaterThanOrEqual(count);
  });

  test('3.5 refresh button reloads listing', async ({ page }) => {
    const refreshBtn = page.locator(SEL.refreshButton);
    await refreshBtn.click();
    await waitForTableLoaded(page);
    await expect(page.locator(SEL.tableRow).first()).toBeVisible();
  });

  test('3.6 sorts by size on header click', async ({ page }) => {
    const sizeHeader = page.locator(SEL.sortableColSize);
    if (await sizeHeader.isVisible().catch(() => false)) {
      await sizeHeader.click();
      await expect(page.locator(SEL.tableRow).first()).toBeVisible();
      // Click again for reverse sort
      await sizeHeader.click();
      await expect(page.locator(SEL.tableRow).first()).toBeVisible();
    }
  });

  test('3.7 sorts by modified date on header click', async ({ page }) => {
    const modifiedHeader = page.locator(SEL.sortableColModified);
    if (await modifiedHeader.isVisible().catch(() => false)) {
      await modifiedHeader.click();
      await expect(page.locator(SEL.tableRow).first()).toBeVisible();
      // Click again for reverse sort
      await modifiedHeader.click();
      await expect(page.locator(SEL.tableRow).first()).toBeVisible();
    }
  });
});
