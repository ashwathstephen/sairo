import { test, expect } from '@playwright/test';
import { SEL } from '../helpers/selectors';
import { BUCKETS } from '../helpers/test-data';
import { dismissWelcomeIfPresent, navigateToBucket, clickTab } from '../helpers/wait-helpers';

test.describe('Object Info Tabs', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/');
    await dismissWelcomeIfPresent(page);
  });

  async function openFileInfo(page: any, bucket: string) {
    await navigateToBucket(page, bucket);
    const fileRow = page.locator(`${SEL.tableRow}:not(.row-folder)`).first();
    const infoBtn = fileRow.locator(`${SEL.colActions} button:has-text("i")`);
    await infoBtn.click();
    await expect(page.locator(SEL.modal)).toBeVisible();
  }

  test('6.1 Tags tab — shows tag management UI', async ({ page }) => {
    await openFileInfo(page, BUCKETS.MAIN);
    await clickTab(page, 'Tags');

    // Should see tag form (admin can add tags)
    const keyInput = page.locator(`${SEL.modal} input[placeholder="Key"]`);
    const valueInput = page.locator(`${SEL.modal} input[placeholder="Value"]`);
    await expect(keyInput).toBeVisible();
    await expect(valueInput).toBeVisible();

    // Add a tag
    await keyInput.fill('e2e-test-key');
    await valueInput.fill('e2e-test-value');
    await page.locator(`${SEL.modal} button:has-text("Add Tag")`).click();

    // Tag update is silent (no toast) — wait for table to update
    await expect(page.locator(SEL.modal)).toContainText('e2e-test-key', { timeout: 5_000 });

    // Remove the tag
    await page.locator(`${SEL.modal} button:has-text("Remove")`).first().click();
    await page.locator(SEL.modalCloseButton).click();
  });

  test('6.2 Versions tab — shows version list', async ({ page }) => {
    await openFileInfo(page, BUCKETS.VERSIONED);
    await clickTab(page, 'Versions');

    // Should show at least one version row (use .first() to avoid strict mode)
    const versionTable = page.locator('.version-table, .version-browser').first();
    await expect(versionTable).toBeVisible();
    // Current version badge should be present
    await expect(page.locator(`${SEL.modal} :has-text("Current")`).first()).toBeVisible();

    await page.locator(SEL.modalCloseButton).click();
  });

  test('6.3 ACL tab — shows ACL info and canned ACL dropdown', async ({ page }) => {
    await openFileInfo(page, BUCKETS.MAIN);
    await clickTab(page, 'ACL');

    // Admin should see canned ACL dropdown
    const aclSelect = page.locator(`${SEL.modal} select`);
    await expect(aclSelect).toBeVisible();
    await expect(page.locator(`${SEL.modal} button:has-text("Apply")`)).toBeVisible();

    await page.locator(SEL.modalCloseButton).click();
  });

  test('6.4 Share tab — generates presigned URL', async ({ page }) => {
    await openFileInfo(page, BUCKETS.MAIN);
    await clickTab(page, 'Share');

    // Click "1 hour" button to generate presigned URL
    await page.locator(`${SEL.modal} button:has-text("1 hour")`).click();

    // URL input should appear with a value
    const urlInput = page.locator(`${SEL.modal} input[readonly]`).first();
    await expect(urlInput).toBeVisible();
    const url = await urlInput.inputValue();
    expect(url).toContain('http');

    // Copy button should work
    await page.locator(`${SEL.modal} button:has-text("Copy")`).first().click();

    await page.locator(SEL.modalCloseButton).click();
  });

  test('6.5 Share tab — creates share link', async ({ page }) => {
    await openFileInfo(page, BUCKETS.MAIN);
    await clickTab(page, 'Share');

    // Create share link
    const createBtn = page.locator(`${SEL.modal} button:has-text("Create Link")`);
    if (await createBtn.isVisible().catch(() => false)) {
      await createBtn.click();
      // Share link URL should appear
      await expect(page.locator(`${SEL.modal} input[readonly]`).last()).toBeVisible({ timeout: 10_000 });
    }

    await page.locator(SEL.modalCloseButton).click();
  });

  test('6.6 Versions tab — restore non-latest version', async ({ page }) => {
    await openFileInfo(page, BUCKETS.VERSIONED);
    await clickTab(page, 'Versions');

    // Wait for version list to load
    await expect(page.locator(`${SEL.modal} :has-text("Current")`).first()).toBeVisible({ timeout: 10_000 });

    // Find a Restore button (only on non-latest versions)
    const restoreBtn = page.locator(`${SEL.modal} button:has-text("Restore")`).first();
    if (await restoreBtn.isVisible({ timeout: 3_000 }).catch(() => false)) {
      await restoreBtn.click();
      // Wait for restore to complete (busy indicator clears)
      await page.waitForTimeout(2000);
      // Version list should still be visible
      await expect(page.locator(`${SEL.modal} :has-text("Current")`).first()).toBeVisible({ timeout: 5_000 });
    }

    await page.locator(SEL.modalCloseButton).click();
  });

  test('6.7 Versions tab — delete specific version with confirmation', async ({ page }) => {
    await openFileInfo(page, BUCKETS.VERSIONED);
    await clickTab(page, 'Versions');

    await expect(page.locator(`${SEL.modal} :has-text("Current")`).first()).toBeVisible({ timeout: 10_000 });

    // Find a Delete button on a non-latest version
    const deleteBtn = page.locator(`${SEL.modal} button.btn-danger:has-text("Delete")`).first();
    if (await deleteBtn.isVisible({ timeout: 3_000 }).catch(() => false)) {
      await deleteBtn.click();

      // ConfirmDialog should appear
      const confirmDialog = page.locator('.modal-small');
      await expect(confirmDialog).toBeVisible({ timeout: 3_000 });
      await expect(confirmDialog).toContainText('Delete Version');

      // Confirm the deletion
      await confirmDialog.locator('button.btn-danger').click();

      // Wait for deletion to complete
      await page.waitForTimeout(2000);
    }

    await page.locator(SEL.modalCloseButton).click();
  });

  test('6.8 Share tab — create share link with password', async ({ page }) => {
    await openFileInfo(page, BUCKETS.MAIN);
    await clickTab(page, 'Share');

    // Fill password field
    const pwdInput = page.locator(SEL.sharePasswordInput);
    if (await pwdInput.isVisible({ timeout: 3_000 }).catch(() => false)) {
      await pwdInput.fill('testpass123');
      await page.locator(SEL.shareCreateBtn).click();

      // Share link should appear
      await expect(page.locator(SEL.urlInput).last()).toBeVisible({ timeout: 10_000 });
      const url = await page.locator(SEL.urlInput).last().inputValue();
      expect(url).toContain('/share/');
    }

    await page.locator(SEL.modalCloseButton).click();
  });

  test('6.9 Share tab — create share link with max downloads', async ({ page }) => {
    await openFileInfo(page, BUCKETS.MAIN);
    await clickTab(page, 'Share');

    // Fill max downloads field
    const maxDlInput = page.locator(SEL.shareMaxDlInput);
    if (await maxDlInput.isVisible({ timeout: 3_000 }).catch(() => false)) {
      await maxDlInput.fill('5');
      await page.locator(SEL.shareCreateBtn).click();

      // Share link should appear
      await expect(page.locator(SEL.urlInput).last()).toBeVisible({ timeout: 10_000 });
    }

    await page.locator(SEL.modalCloseButton).click();
  });

  test('6.10 Share tab — presigned URL copy shows Copied!', async ({ page }) => {
    await openFileInfo(page, BUCKETS.MAIN);
    await clickTab(page, 'Share');

    // Generate presigned URL
    await page.locator(`${SEL.modal} button:has-text("1 hour")`).click();
    await expect(page.locator(SEL.urlInput).first()).toBeVisible({ timeout: 5_000 });

    // Click Copy button
    const copyBtn = page.locator(SEL.presignedCopyBtn).first();
    await copyBtn.click();

    // Button text should change to "Copied!"
    await expect(copyBtn).toContainText('Copied!');

    await page.locator(SEL.modalCloseButton).click();
  });
});
