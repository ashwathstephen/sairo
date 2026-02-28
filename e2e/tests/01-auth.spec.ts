import { test, expect } from '@playwright/test';
import { SEL } from '../helpers/selectors';
import { ADMIN } from '../helpers/test-data';
import { dismissWelcomeIfPresent } from '../helpers/wait-helpers';

test.describe('Authentication & Session', () => {
  test.use({ storageState: { cookies: [], origins: [] } }); // No auth for login tests

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
