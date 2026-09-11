import { test, expect } from '@playwright/test';
import { SEL } from '../helpers/selectors';
import { loginAsAdmin } from '../helpers/wait-helpers';

// Issue #29: a user changes their own password from the header, and the new password works.
test.describe('Change own password', () => {
  test.describe.configure({ mode: 'serial' });
  // These tests sign in and out themselves, so they must start from a signed-out
  // browser. The chromium project otherwise loads a stored admin session and the
  // login form is never rendered.
  test.use({ storageState: { cookies: [], origins: [] } });

  async function changePassword(page, current: string, next: string) {
    await page.locator(SEL.passwordHeaderButton).click();
    await expect(page.locator(SEL.modal)).toBeVisible();
    await page.getByLabel('Current password', { exact: true }).fill(current);
    await page.getByLabel('New password', { exact: true }).fill(next);
    await page.getByLabel('Confirm new password', { exact: true }).fill(next);
    await page.locator(SEL.modal).getByRole('button', { name: 'Update Password' }).click();
    await expect(page.locator(SEL.modal)).toContainText('has been updated');
    await page.locator(SEL.modal).getByRole('button', { name: 'Done' }).click();
  }

  test('29.1 wrong current password is rejected in place', async ({ page }) => {
    await loginAsAdmin(page);
    await page.locator(SEL.passwordHeaderButton).click();
    await page.getByLabel('Current password', { exact: true }).fill('not-the-password');
    await page.getByLabel('New password', { exact: true }).fill('brandnewpass1');
    await page.getByLabel('Confirm new password', { exact: true }).fill('brandnewpass1');
    await page.locator(SEL.modal).getByRole('button', { name: 'Update Password' }).click();
    await expect(page.locator(SEL.modal)).toContainText('Current password is incorrect');
  });

  test('29.2 new password logs in; old one does not; then restore', async ({ page }) => {
    await loginAsAdmin(page);
    await changePassword(page, 'password', 'brandnewpass1');
    await page.locator(SEL.logoutButton).click();
    await expect(page.locator(SEL.signInButton)).toBeVisible();
    await page.locator(SEL.usernameInput).fill('admin');
    await page.locator(SEL.passwordInput).fill('password');
    await page.locator(SEL.signInButton).click();
    await expect(page.locator('.login-error')).toBeVisible();
    await page.locator(SEL.passwordInput).fill('brandnewpass1');
    await page.locator(SEL.signInButton).click();
    await expect(page.locator(SEL.bucketCard).first()).toBeVisible({ timeout: 15_000 });
    await changePassword(page, 'brandnewpass1', 'password');   // leave the stack as we found it
  });

  // Issue #54: the Password button sits in the header, which renders in every view, but the dialog
  // itself was only mounted in the bucket-list branch — so inside a bucket the button set state and
  // nothing appeared.
  test('29.3 opens from inside a bucket, not just the overview', async ({ page }) => {
    await loginAsAdmin(page);
    await page.locator(SEL.bucketCard).first().click();
    await expect(page.locator(SEL.passwordHeaderButton)).toBeVisible();
    await page.locator(SEL.passwordHeaderButton).click();
    await expect(page.locator(SEL.modal)).toBeVisible();
    await expect(page.locator(SEL.modal)).toContainText('Change Password');
    await expect(page.getByLabel('Current password', { exact: true })).toBeVisible();
  });

  // The fields used bare <input>, which this stylesheet gives no base style, so they rendered with
  // browser-default chrome and kept black text on the dark modal background.
  test('29.4 fields are styled and readable in dark mode', async ({ page }) => {
    await loginAsAdmin(page);
    await page.emulateMedia({ colorScheme: 'dark' });
    await page.evaluate(() => document.documentElement.setAttribute('data-theme', 'dark'));
    await page.locator(SEL.passwordHeaderButton).click();
    await expect(page.locator(SEL.modal)).toBeVisible();
    const field = page.getByLabel('Current password', { exact: true });
    const seen = await field.evaluate((el) => {
      const s = getComputedStyle(el);
      const lum = (c: string) => {
        const [r, g, b] = (c.match(/[\d.]+/g) || ['0', '0', '0']).map(Number);
        return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255;
      };
      return { text: lum(s.color), bg: lum(s.backgroundColor), radius: s.borderTopLeftRadius, pad: s.paddingLeft };
    });
    // text and background must not both be dark, or the value is invisible
    expect(Math.abs(seen.text - seen.bg)).toBeGreaterThan(0.4);
    expect(seen.radius).not.toBe('0px');   // styled, not browser-default
    expect(seen.pad).not.toBe('0px');
  });
});
