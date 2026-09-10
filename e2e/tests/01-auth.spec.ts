import { test, expect } from '@playwright/test';
import { SEL } from '../helpers/selectors';
import { ADMIN } from '../helpers/test-data';
import { dismissWelcomeIfPresent } from '../helpers/wait-helpers';

test.describe('Authentication & Session', () => {
  test.use({ storageState: { cookies: [], origins: [] } }); // No auth for login tests

  test('1.6 LDAP is selectable and does not fall back to local auth', async ({ page }) => {
    // #35: the LDAP option vanished from the login form. It is back, so prove two
    // things in a real browser: the control renders, and choosing it really sends
    // the request to the LDAP endpoint. The test stack points at an unreachable
    // directory, so a correct local password must still be rejected here — if it
    // logged in, the form would be silently falling back to local auth.
    await page.goto('/');
    const ldapToggle = page.getByLabel('Sign in with LDAP');
    await expect(ldapToggle).toBeVisible();

    const ldapCall = page.waitForRequest(r => r.url().includes('/api/auth/ldap') && r.method() === 'POST');
    await ldapToggle.check();
    await expect(page.locator(SEL.signInButton)).toContainText('LDAP');
    await page.locator(SEL.usernameInput).fill(ADMIN.username);
    await page.locator(SEL.passwordInput).fill(ADMIN.password);
    await page.locator(SEL.signInButton).click();

    await ldapCall;                                   // it used the LDAP route
    await expect(page.locator(SEL.loginError)).toBeVisible();
    await expect(page.locator(SEL.loginForm)).toBeVisible();
    await expect(page.locator(SEL.bucketCard)).toHaveCount(0);
  });

  test('1.1 shows error on wrong password', async ({ page }) => {
    await page.goto('/');
    await page.locator(SEL.usernameInput).fill(ADMIN.username);
    await page.locator(SEL.passwordInput).fill('wrongpassword');
    await page.locator(SEL.signInButton).click();
    await expect(page.locator(SEL.loginError)).toBeVisible();
  });

  test('1.1 logs in with correct credentials and shows bucket list', async ({ page }) => {
    await page.goto('/');
    await page.locator(SEL.usernameInput).fill(ADMIN.username);
    await page.locator(SEL.passwordInput).fill(ADMIN.password);
    await page.locator(SEL.signInButton).click();
    await expect(page.locator(SEL.bucketCard).first()).toBeVisible({ timeout: 15_000 });
  });

  test('1.1 session persists on page refresh', async ({ page }) => {
    await page.goto('/');
    await page.locator(SEL.usernameInput).fill(ADMIN.username);
    await page.locator(SEL.passwordInput).fill(ADMIN.password);
    await page.locator(SEL.signInButton).click();
    await expect(page.locator(SEL.bucketCard).first()).toBeVisible({ timeout: 15_000 });

    // Refresh
    await page.reload();
    // Should still see bucket list, not login form
    await expect(page.locator(SEL.bucketCard).first()).toBeVisible({ timeout: 15_000 });
    await expect(page.locator(SEL.loginForm)).toBeHidden();
  });

  test('1.2 logout returns to login page', async ({ page }) => {
    await page.goto('/');
    await page.locator(SEL.usernameInput).fill(ADMIN.username);
    await page.locator(SEL.passwordInput).fill(ADMIN.password);
    await page.locator(SEL.signInButton).click();
    await expect(page.locator(SEL.bucketCard).first()).toBeVisible({ timeout: 15_000 });
    await dismissWelcomeIfPresent(page);

    // Logout
    await page.locator(SEL.logoutButton).click();
    await expect(page.locator(SEL.loginForm)).toBeVisible();
  });

  test('1.2 refresh after logout stays on login page', async ({ page }) => {
    await page.goto('/');
    await page.locator(SEL.usernameInput).fill(ADMIN.username);
    await page.locator(SEL.passwordInput).fill(ADMIN.password);
    await page.locator(SEL.signInButton).click();
    await expect(page.locator(SEL.bucketCard).first()).toBeVisible({ timeout: 15_000 });
    await dismissWelcomeIfPresent(page);

    await page.locator(SEL.logoutButton).click();
    await expect(page.locator(SEL.loginForm)).toBeVisible();

    await page.reload();
    await expect(page.locator(SEL.loginForm)).toBeVisible();
  });
});
