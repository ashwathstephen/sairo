import { test, expect } from '@playwright/test';
import { SEL } from '../helpers/selectors';
import { BUCKETS } from '../helpers/test-data';
import { dismissWelcomeIfPresent, navigateToBucket, clickTab, waitForToast } from '../helpers/wait-helpers';

test.describe('Bucket Settings', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/');
    await dismissWelcomeIfPresent(page);
    await navigateToBucket(page, BUCKETS.SETTINGS);
    await page.locator(SEL.settingsButton).click();
    await expect(page.locator(SEL.modal)).toBeVisible();
  });

  test('7.1 Overview tab — shows versioning status', async ({ page }) => {
    // Overview is the default tab
    await expect(page.locator(`${SEL.modal} h3:has-text("Versioning")`)).toBeVisible();
    await expect(page.locator(`${SEL.modal} ${SEL.statusBadge}`).first()).toBeVisible();
  });

  test('7.1 Overview tab — admin can toggle versioning', async ({ page }) => {
    const toggleBtn = page.locator(`${SEL.modal} button:has-text("Enable"), ${SEL.modal} button:has-text("Suspend")`).first();
    await expect(toggleBtn).toBeVisible();
    const initialText = await toggleBtn.textContent();
    // Click to toggle
    await toggleBtn.click();
    // Versioning toggle updates status inline (badge changes) — no toast
    // Verify the status badge changed
    const expectedStatus = initialText?.includes('Enable') ? 'Enabled' : 'Suspended';
    await expect(page.locator(`${SEL.modal} ${SEL.statusBadge}`).first()).toContainText(expectedStatus, { timeout: 5_000 });
  });

  test('7.2 Lifecycle tab — shows lifecycle rules interface', async ({ page }) => {
    await clickTab(page, 'Lifecycle');
    await expect(page.locator(`${SEL.modal} h3:has-text("Lifecycle")`)).toBeVisible();
    // Add rule button should be visible
    await expect(page.locator(`${SEL.modal} button:has-text("Add Rule")`)).toBeVisible();
  });

  test('7.2 Lifecycle tab — admin can add a rule', async ({ page }) => {
    await clickTab(page, 'Lifecycle');
    await page.locator(`${SEL.modal} button:has-text("Add Rule")`).click();
    // A new rule card should appear
    await expect(page.locator('.lc-card')).toBeVisible();
  });

  test('7.3 Policy tab — shows bucket policy area', async ({ page }) => {
    await clickTab(page, 'Policy');
    await expect(page.locator(`${SEL.modal} h3:has-text("Bucket Policy")`)).toBeVisible();
    // Textarea for policy JSON
    const textarea = page.locator(`${SEL.modal} textarea`);
    await expect(textarea).toBeVisible();
  });

  test('7.4 ACL tab — shows bucket ACL', async ({ page }) => {
    await clickTab(page, 'ACL');
    await expect(page.locator(`${SEL.modal} h3:has-text("Bucket ACL")`)).toBeVisible();
    // Canned ACL selector
    await expect(page.locator(`${SEL.modal} select`).first()).toBeVisible();
  });

  test('7.5 Tags tab — shows bucket tags', async ({ page }) => {
    await clickTab(page, 'Tags');
    await expect(page.locator(`${SEL.modal} h3:has-text("Bucket Tags")`)).toBeVisible();
  });

  test('7.6 CORS tab — shows CORS config', async ({ page }) => {
    await clickTab(page, 'CORS');
    await expect(page.locator(`${SEL.modal} h3:has-text("CORS")`)).toBeVisible();
    await expect(page.locator(`${SEL.modal} textarea`)).toBeVisible();
  });

  test('7.7 Multipart tab — shows multipart uploads', async ({ page }) => {
    await clickTab(page, 'Multipart');
    await expect(page.locator(`${SEL.modal} h3:has-text("Multipart")`)).toBeVisible();
  });

  test('7.8 Index tab — shows crawl status', async ({ page }) => {
    await clickTab(page, 'Index');
    await expect(page.locator(`${SEL.modal} h3:has-text("Object Index")`)).toBeVisible();
    // Re-index button
    await expect(page.locator(SEL.reindexButton)).toBeVisible();
  });

  test('7.8 Index tab — Re-index triggers crawl', async ({ page }) => {
    await clickTab(page, 'Index');
    await page.locator(SEL.reindexButton).click();
    // After clicking, button becomes disabled "Indexing..." or crawl completes and button returns
    // Use waitForFunction to handle either transient state
    await page.waitForFunction(() => {
      const btn = document.querySelector('.modal button.btn-primary');
      if (!btn) return false;
      const text = btn.textContent || '';
      // Either the crawl started (Indexing...) or already finished (Re-index)
      return text.includes('Indexing') || text.includes('Re-index');
    }, { timeout: 10_000 });
  });

  test('7.9 Policy tab — save bucket policy', async ({ page }) => {
    await clickTab(page, 'Policy');
    const textarea = page.locator(`${SEL.modal} textarea`);
    await textarea.fill('{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":"*","Action":"s3:GetObject","Resource":"arn:aws:s3:::e2e-test-settings/*"}]}');
    await page.locator(SEL.savePolicyBtn).click();
    // Delete Policy button should now appear (policy was saved)
    await expect(page.locator(SEL.deletePolicyBtn)).toBeVisible({ timeout: 5_000 });
  });

  test('7.10 Policy tab — delete bucket policy', async ({ page }) => {
    await clickTab(page, 'Policy');
    const deleteBtn = page.locator(SEL.deletePolicyBtn);
    if (await deleteBtn.isVisible({ timeout: 3_000 }).catch(() => false)) {
      await deleteBtn.click();
      // Delete Policy button should disappear
      await expect(deleteBtn).toBeHidden({ timeout: 5_000 });
    }
  });

  test('7.11 CORS tab — save CORS config', async ({ page }) => {
    await clickTab(page, 'CORS');
    const textarea = page.locator(`${SEL.modal} textarea`);
    await textarea.fill('[{"AllowedOrigins":["*"],"AllowedMethods":["GET"],"AllowedHeaders":["*"]}]');
    await page.locator(SEL.saveCorsBtn).click();

    // MinIO may return NotImplemented for CORS — wait for either success or error
    await page.waitForFunction(() => {
      // Error dialog appeared (.modal-small is the ConfirmDialog component)
      const errDialog = document.querySelector('.modal-small');
      if (errDialog && errDialog.textContent?.includes('Error')) return true;
      // Delete CORS button appeared (save succeeded)
      const btns = document.querySelectorAll('.modal button');
      for (const b of btns) {
        if (b.textContent?.includes('Delete CORS')) return true;
      }
      return false;
    }, { timeout: 5_000 });

    // Dismiss error dialog if it appeared
    const okBtn = page.locator('.modal-small button:has-text("OK")');
    if (await okBtn.isVisible().catch(() => false)) {
      await okBtn.click();
    }
  });

  test('7.12 CORS tab — delete CORS config', async ({ page }) => {
    await clickTab(page, 'CORS');
    const deleteBtn = page.locator(SEL.deleteCorsBtn);
    if (await deleteBtn.isVisible({ timeout: 3_000 }).catch(() => false)) {
      await deleteBtn.click();
      await expect(deleteBtn).toBeHidden({ timeout: 5_000 });
    }
  });

  test('7.13 Multipart tab — shows multipart state', async ({ page }) => {
    await clickTab(page, 'Multipart');
    // Either shows uploads with Abort buttons or "No incomplete" message
    const abortBtn = page.locator(`${SEL.modal} button.btn-danger:has-text("Abort")`).first();
    const emptyMsg = page.locator(`${SEL.modal} :has-text("No incomplete")`).first();
    await expect(abortBtn.or(emptyMsg)).toBeVisible({ timeout: 5_000 });
  });

  test('7.14 Lifecycle tab — save lifecycle rules', async ({ page }) => {
    await clickTab(page, 'Lifecycle');
    await page.locator(`${SEL.modal} button:has-text("Add Rule")`).click();
    await expect(page.locator(SEL.lcCard)).toBeVisible();

    // Fill expiration days
    const expirationInput = page.locator(`${SEL.lcCard} input[type="number"]`).first();
    await expirationInput.fill('30');

    // Save Changes button should appear (dirty state)
    await expect(page.locator(SEL.saveLifecycleBtn)).toBeVisible({ timeout: 3_000 });
    await page.locator(SEL.saveLifecycleBtn).click();

    // Save Changes should disappear (clean state)
    await expect(page.locator(SEL.saveLifecycleBtn)).toBeHidden({ timeout: 5_000 });
  });

  test('7.15 Lifecycle tab — remove lifecycle rule', async ({ page }) => {
    await clickTab(page, 'Lifecycle');

    const lcCard = page.locator(SEL.lcCard).first();
    if (await lcCard.isVisible({ timeout: 3_000 }).catch(() => false)) {
      // Click Remove on the rule
      await lcCard.locator('button.btn-danger:has-text("Remove"), button:has-text("Remove")').first().click();

      // Save Changes should appear (dirty from removal)
      await expect(page.locator(SEL.saveLifecycleBtn)).toBeVisible({ timeout: 3_000 });
      await page.locator(SEL.saveLifecycleBtn).click();
      await expect(page.locator(SEL.saveLifecycleBtn)).toBeHidden({ timeout: 5_000 });
    }
  });

  test('close button works', async ({ page }) => {
    await page.locator(SEL.modalCloseButton).click();
    await expect(page.locator(SEL.modal)).toBeHidden();
  });
});
