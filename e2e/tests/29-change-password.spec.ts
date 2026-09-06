import { test, expect } from '@playwright/test';
import { SEL } from '../helpers/selectors';
import { loginAsAdmin } from '../helpers/wait-helpers';

// Issue #29: a user changes their own password from the header, and the new password works.
test.describe('Change own password', () => {
  test.describe.configure({ mode: 'serial' });

  async function changePassword(page, current: string, next: string) {
    await page.locator(SEL.passwordHeaderButton).click();
    await expect(page.locator(SEL.modal)).toBeVisible();
    await page.locator('input[aria-label="Current password"]').fill(current);
    await page.locator('input[aria-label="New password"]').fill(next);
    await page.locator('input[aria-label="Confirm new password"]').fill(next);
    await page.locator(SEL.modal).getByRole('button', { name: 'Update Password' }).click();
    await expect(page.locator(SEL.modal)).toContainText('has been updated');
    await page.locator(SEL.modal).getByRole('button', { name: 'Done' }).click();
  }

  test('29.1 wrong current password is rejected in place', async ({ page }) => {
    await loginAsAdmin(page);
    await page.locator(SEL.passwordHeaderButton).click();
    await page.locator('input[aria-label="Current password"]').fill('not-the-password');
    await page.locator('input[aria-label="New password"]').fill('brandnewpass1');
    await page.locator('input[aria-label="Confirm new password"]').fill('brandnewpass1');
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
});
