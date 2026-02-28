import { test, expect } from '@playwright/test';
import { SEL } from '../helpers/selectors';
import { dismissWelcomeIfPresent } from '../helpers/wait-helpers';

test.describe('License Management', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/');
    await dismissWelcomeIfPresent(page);
  });

  test('13 opens LicenseManager from License button', async ({ page }) => {
    await page.locator(SEL.licenseButton).click();
    await expect(page.locator(SEL.modal)).toBeVisible();
  });

  test('13 shows Community license type', async ({ page }) => {
    await page.locator(SEL.licenseButton).click();
    await expect(page.locator(`${SEL.modal} :has-text("Community")`).first()).toBeVisible();
  });

  test('13 license key input and Activate button present', async ({ page }) => {
    await page.locator(SEL.licenseButton).click();
    await expect(page.locator(`${SEL.modal} input`).first()).toBeVisible();
    await expect(page.locator(`${SEL.modal} button:has-text("Activate")`)).toBeVisible();
  });

  test('13 invalid key shows error', async ({ page }) => {
    await page.locator(SEL.licenseButton).click();
    await page.locator(`${SEL.modal} input`).first().fill('invalid-license-key-123');
    await page.locator(`${SEL.modal} button:has-text("Activate")`).click();

    // Error shown as inline red text (not .form-error class)
    await expect(page.locator(`${SEL.modal} :has-text("Invalid")`).first()).toBeVisible({ timeout: 5_000 });
  });

  test('13 close button works', async ({ page }) => {
    await page.locator(SEL.licenseButton).click();
    await page.locator(SEL.modalCloseButton).click();
    await expect(page.locator(SEL.modal)).toBeHidden();
  });
});
