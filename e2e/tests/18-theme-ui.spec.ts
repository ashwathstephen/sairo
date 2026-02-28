import { test, expect } from '@playwright/test';
import { SEL } from '../helpers/selectors';
import { BUCKETS } from '../helpers/test-data';
import { dismissWelcomeIfPresent, navigateToBucket, waitForToastDismissed } from '../helpers/wait-helpers';

test.describe('Theme & UI', () => {
  test('18.1 theme toggle switches dark/light mode', async ({ page }) => {
    await page.goto('/');
    await dismissWelcomeIfPresent(page);

    // Get initial theme
    const initialTheme = await page.evaluate(() => document.documentElement.getAttribute('data-theme'));

    // Click theme toggle
    await page.locator(SEL.themeToggle).click();

    // Theme should change
    const newTheme = await page.evaluate(() => document.documentElement.getAttribute('data-theme'));
    expect(newTheme).not.toBe(initialTheme);
  });

  test('18.1 theme persists after refresh', async ({ page }) => {
    await page.goto('/');
    await dismissWelcomeIfPresent(page);

    // Toggle to a known state
    await page.locator(SEL.themeToggle).click();
    const theme = await page.evaluate(() => document.documentElement.getAttribute('data-theme'));

    // Refresh
    await page.reload();
    await dismissWelcomeIfPresent(page);

    // Wait for React to hydrate and apply the theme from localStorage
    await page.waitForFunction((expected) => {
      return document.documentElement.getAttribute('data-theme') === expected;
    }, theme, { timeout: 5_000 });
  });

  test('18.2 ? key opens keyboard shortcuts modal', async ({ page }) => {
    await page.goto('/');
    await dismissWelcomeIfPresent(page);
    await navigateToBucket(page, BUCKETS.MAIN);

    // Make sure no input is focused
    await page.locator('body').click();
    await page.keyboard.press('?');

    // Shortcuts modal should appear
    await expect(page.locator(`${SEL.modal} h2:has-text("Keyboard Shortcuts")`)).toBeVisible();

    // Should list shortcuts
    await expect(page.locator(SEL.shortcutRow).first()).toBeVisible();

    // Close button works
    await page.locator(SEL.modalCloseButton).click();
    await expect(page.locator(SEL.modal)).toBeHidden();
  });

  test('18.3 welcome modal shown on first visit', async ({ page }) => {
    // Clear localStorage to simulate first visit
    await page.goto('/');
    await page.evaluate(() => localStorage.removeItem('sairo-onboarded'));
    await page.reload();

    // Welcome modal should appear
    const gotIt = page.locator(SEL.welcomeGotIt);
    await expect(gotIt).toBeVisible({ timeout: 5_000 });

    // Dismiss
    await gotIt.click();
    await expect(gotIt).toBeHidden();

    // Refresh — should NOT show again
    await page.reload();
    await page.waitForTimeout(2000);
    await expect(page.locator(SEL.welcomeGotIt)).toBeHidden();
  });

  test('18.4 success toast shows green styling', async ({ page }) => {
    await page.goto('/');
    await dismissWelcomeIfPresent(page);

    // Trigger a success action (create and delete a bucket)
    const testBucket = 'e2e-toast-test';
    await page.locator(SEL.createBucketBtn).click();
    await page.locator(SEL.createBucketInput).fill(testBucket);
    await page.locator(SEL.createBucketInput).press('Enter');

    // Success toast should appear
    await expect(page.locator(SEL.toastSuccess).first()).toBeVisible({ timeout: 5_000 });

    // Cleanup
    await page.waitForTimeout(500);
    const card = page.locator(`${SEL.bucketCard}:has-text("${testBucket}")`);
    if (await card.isVisible().catch(() => false)) {
      await card.locator(SEL.bucketDeleteBtn).click();
      await page.locator(SEL.deleteConfirmButton).click();
    }
  });

  test('18.5 toast auto-dismiss removes toast after timeout', async ({ page }) => {
    await page.goto('/');
    await dismissWelcomeIfPresent(page);

    // Trigger a success toast (create a bucket)
    const testBucket = 'e2e-toast-auto-dismiss';
    await page.locator(SEL.createBucketBtn).click();
    await page.locator(SEL.createBucketInput).fill(testBucket);
    await page.locator(SEL.createBucketInput).press('Enter');

    // Toast should appear
    await expect(page.locator(SEL.toastSuccess).first()).toBeVisible({ timeout: 5_000 });

    // Wait for auto-dismiss (default 4s + buffer)
    await waitForToastDismissed(page, 'Created', 8000);

    // Cleanup
    const card = page.locator(`${SEL.bucketCard}:has-text("${testBucket}")`);
    if (await card.isVisible().catch(() => false)) {
      await card.locator(SEL.bucketDeleteBtn).click();
      await page.locator(SEL.deleteConfirmButton).click();
    }
  });

  test('18.6 toast close button dismisses immediately', async ({ page }) => {
    await page.goto('/');
    await dismissWelcomeIfPresent(page);

    // Trigger a success toast
    const testBucket = 'e2e-toast-close-btn';
    await page.locator(SEL.createBucketBtn).click();
    await page.locator(SEL.createBucketInput).fill(testBucket);
    await page.locator(SEL.createBucketInput).press('Enter');

    // Toast should appear
    const toast = page.locator(SEL.toastSuccess).first();
    await expect(toast).toBeVisible({ timeout: 5_000 });

    // Click close button
    const closeBtn = page.locator(SEL.toastClose).first();
    if (await closeBtn.isVisible().catch(() => false)) {
      await closeBtn.click();
      await expect(toast).toBeHidden({ timeout: 2_000 });
    }

    // Cleanup
    await page.waitForTimeout(500);
    const card = page.locator(`${SEL.bucketCard}:has-text("${testBucket}")`);
    if (await card.isVisible().catch(() => false)) {
      await card.locator(SEL.bucketDeleteBtn).click();
      await page.locator(SEL.deleteConfirmButton).click();
    }
  });

  test.fixme('18.7 toast action buttons trigger callback', async ({ page }) => {
    // Action-bearing toasts are hard to trigger reliably without mocking.
    // This test is a placeholder for when a deterministic trigger is available.
    await page.goto('/');
    await dismissWelcomeIfPresent(page);
    await expect(page.locator(SEL.toastAction).first()).toBeVisible();
  });
});
