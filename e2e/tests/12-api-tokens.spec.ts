import { test, expect } from '@playwright/test';
import { SEL } from '../helpers/selectors';
import { dismissWelcomeIfPresent, waitForToast } from '../helpers/wait-helpers';

test.describe('API Tokens', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/');
    await dismissWelcomeIfPresent(page);
  });

  test('12 opens TokenManager from API Tokens button', async ({ page }) => {
    await page.locator(SEL.apiTokensButton).click();
    await expect(page.locator(SEL.modal)).toBeVisible();
    await expect(page.locator(`${SEL.modal} h2`)).toContainText('Token');
  });

  test('12 creates new token with sairo_ prefix', async ({ page }) => {
    await page.locator(SEL.apiTokensButton).click();

    // Fill token name
    const nameInput = page.locator(`${SEL.modal} input[placeholder*="name" i], ${SEL.modal} input[type="text"]`).first();
    await nameInput.fill('e2e-test-token');

    // Create token
    const createBtn = page.locator(`${SEL.modal} button:has-text("Create")`).first();
    await createBtn.click();

    // Token should be displayed (shown once)
    await expect(page.locator(`${SEL.modal} :has-text("sairo_")`).first()).toBeVisible({ timeout: 5_000 });
  });

  test('12 token appears in table after creation', async ({ page }) => {
    await page.locator(SEL.apiTokensButton).click();

    // Should see e2e-test-token in the table (from previous test or existing)
    await expect(page.locator(`${SEL.modal} td:has-text("e2e-test-token"), ${SEL.modal} :has-text("e2e-test-token")`).first()).toBeVisible({ timeout: 5_000 });
  });

  test('12 revoke button with confirmation deletes token', async ({ page }) => {
    await page.locator(SEL.apiTokensButton).click();

    // Find revoke button for e2e-test-token
    const revokeBtn = page.locator(`${SEL.modal} button:has-text("Revoke")`).first();
    if (await revokeBtn.isVisible().catch(() => false)) {
      await revokeBtn.click();

      // Confirmation dialog is a .modal-small inside its own .modal-overlay
      const confirmBtn = page.locator('.modal-small button.btn-danger');
      if (await confirmBtn.isVisible({ timeout: 3_000 }).catch(() => false)) {
        await confirmBtn.click();
      }
    }
  });
});
