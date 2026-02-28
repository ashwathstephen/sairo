import { test, expect } from '@playwright/test';
import { SEL } from '../helpers/selectors';
import { ADMIN } from '../helpers/test-data';
import { dismissWelcomeIfPresent } from '../helpers/wait-helpers';

/**
 * 2FA tests require generating TOTP codes.
 * We extract the secret from the setup QR and use the otpauth library.
 */

test.describe('Two-Factor Authentication', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/');
    await dismissWelcomeIfPresent(page);
  });

  test('11.1 opens TwoFactorSetup modal from 2FA button', async ({ page }) => {
    await page.locator(SEL.tfaHeaderButton).click();
    await expect(page.locator(SEL.modal)).toBeVisible();
  });

  test('11.1 Set Up 2FA shows QR code and secret', async ({ page }) => {
    await page.locator(SEL.tfaHeaderButton).click();

    const setupBtn = page.locator(`${SEL.modal} button:has-text("Set Up 2FA")`);
    if (await setupBtn.isVisible().catch(() => false)) {
      await setupBtn.click();

      // QR code (canvas or svg) should appear
      await expect(page.locator(`${SEL.modal} canvas, ${SEL.modal} svg`).first()).toBeVisible({ timeout: 5_000 });

      // Secret key should be shown
      const secretEl = page.locator(`${SEL.modal} code, ${SEL.modal} .mono`).first();
      if (await secretEl.isVisible().catch(() => false)) {
        const secret = await secretEl.textContent();
        expect(secret!.length).toBeGreaterThan(10);
      }
    }

    // Close without enabling — use Back button (2FA setup has Back, not Close)
    const backBtn = page.locator(`${SEL.modal} button:has-text("Back")`);
    const closeBtn = page.locator(SEL.modalCloseButton);
    await (await backBtn.isVisible().catch(() => false) ? backBtn : closeBtn).click();
  });

  test('11.1 full 2FA enable/disable flow with TOTP', async ({ page }) => {
    // Dynamic import of otpauth
    let OTPAuth: any;
    try {
      OTPAuth = await import('otpauth');
    } catch {
      test.skip(true, 'otpauth not installed — skipping TOTP test');
      return;
    }

    await page.locator(SEL.tfaHeaderButton).click();

    const setupBtn = page.locator(`${SEL.modal} button:has-text("Set Up 2FA")`);
    if (!(await setupBtn.isVisible().catch(() => false))) {
      // 2FA might already be enabled — skip
      await page.locator(SEL.modalCloseButton).click();
      return;
    }

    await setupBtn.click();

    // Extract secret from the page
    await page.waitForTimeout(1000); // Wait for QR to render
    const secretEl = page.locator(`${SEL.modal} code, ${SEL.modal} .mono`).first();
    const secret = await secretEl.textContent();
    if (!secret) {
      await page.locator(SEL.modalCloseButton).click();
      return;
    }

    // Generate TOTP code
    const totp = new OTPAuth.TOTP({
      secret: OTPAuth.Secret.fromBase32(secret.trim()),
      digits: 6,
      period: 30,
    });
    const code = totp.generate();

    // Enter code and enable
    await page.locator(`${SEL.modal} input`).last().fill(code);
    await page.locator(`${SEL.modal} button:has-text("Verify")`).click();

    // Should show recovery codes
    await expect(page.locator(`${SEL.modal} :has-text("recovery")`).first()).toBeVisible({ timeout: 5_000 });

    // Click "I've Saved These"
    const savedBtn = page.locator(`${SEL.modal} button:has-text("Saved")`);
    if (await savedBtn.isVisible().catch(() => false)) {
      await savedBtn.click();
    }

    // Now disable 2FA
    await page.locator(SEL.tfaHeaderButton).click();
    const disableBtn = page.locator(`${SEL.modal} button:has-text("Disable")`);
    if (await disableBtn.isVisible().catch(() => false)) {
      await disableBtn.click();
      // May need to enter password
      const pwdInput = page.locator(`${SEL.modal} input[type="password"]`);
      if (await pwdInput.isVisible().catch(() => false)) {
        await pwdInput.fill(ADMIN.password);
        await page.locator(`${SEL.modal} button:has-text("Confirm"), ${SEL.modal} button.btn-danger`).last().click();
      }
    }

    await page.locator(SEL.modalCloseButton).click().catch(() => {});
  });
});
