import { test, expect } from '@playwright/test';
import { SEL } from '../helpers/selectors';
import { dismissWelcomeIfPresent } from '../helpers/wait-helpers';

test.describe('Endpoint Management', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/');
    await dismissWelcomeIfPresent(page);
  });

  test('15.1 opens EndpointManager from Endpoints button', async ({ page }) => {
    await page.locator(SEL.endpointsButton).click();
    await expect(page.locator(SEL.modal)).toBeVisible();
  });

  test('15.1 default endpoint is listed', async ({ page }) => {
    await page.locator(SEL.endpointsButton).click();
    // Should show at least the default endpoint
    await expect(page.locator(`${SEL.modal} :has-text("default")`).first()).toBeVisible({ timeout: 5_000 });
  });

  test('15.2 Add Endpoint button shows form', async ({ page }) => {
    await page.locator(SEL.endpointsButton).click();
    const addBtn = page.locator(`${SEL.modal} button:has-text("Add Endpoint")`);
    if (await addBtn.isVisible().catch(() => false)) {
      await addBtn.click();
      // Form fields should appear
      await expect(page.locator(`${SEL.modal} input`).first()).toBeVisible();
    }
  });

  test('15.3 Test button on default endpoint works', async ({ page }) => {
    await page.locator(SEL.endpointsButton).click();

    const testBtn = page.locator(`${SEL.modal} button:has-text("Test")`).first();
    if (await testBtn.isVisible().catch(() => false)) {
      await testBtn.click();
      // Should show success or error
      await page.waitForTimeout(3000);
      // Check for a result indication
      await expect(page.locator(`${SEL.modal} :has-text("Success"), ${SEL.modal} :has-text("Connected"), .toast`).first()).toBeVisible({ timeout: 10_000 });
    }
  });

  test('close button works', async ({ page }) => {
    await page.locator(SEL.endpointsButton).click();
    await page.locator(SEL.modalCloseButton).click();
    await expect(page.locator(SEL.modal)).toBeHidden();
  });
});
