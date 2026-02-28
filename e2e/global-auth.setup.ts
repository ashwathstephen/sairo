import { test as setup, expect } from '@playwright/test';
import { SEL } from './helpers/selectors';
import { ADMIN } from './helpers/test-data';

/**
 * Auth setup project: logs in as admin and saves storageState
 * so all subsequent tests start authenticated.
 */
setup('authenticate as admin', async ({ page }) => {
  await page.goto('/');

  // Fill login form
  await page.locator(SEL.usernameInput).fill(ADMIN.username);
  await page.locator(SEL.passwordInput).fill(ADMIN.password);
  await page.locator(SEL.signInButton).click();

  // Wait for either bucket cards or Welcome modal (Welcome appears on first login)
  await Promise.race([
    page.locator(SEL.bucketCard).first().waitFor({ state: 'visible', timeout: 15_000 }),
    page.locator(SEL.welcomeGotIt).waitFor({ state: 'visible', timeout: 15_000 }),
  ]);

  // Dismiss welcome modal if present
  const gotIt = page.locator(SEL.welcomeGotIt);
  if (await gotIt.isVisible({ timeout: 2000 }).catch(() => false)) {
    await gotIt.click();
    await expect(gotIt).toBeHidden();
  }

  // Confirm bucket cards are visible after dismissal
  await expect(page.locator(SEL.bucketCard).first()).toBeVisible({ timeout: 10_000 });

  // Save storage state (cookies + localStorage, including sairo-onboarded flag)
  await page.context().storageState({ path: './auth-state.json' });
});
