import { test, expect } from '@playwright/test';
import { dismissWelcomeIfPresent } from '../helpers/wait-helpers';

test.describe('Session Timeout', () => {
  test.skip('24.1 session timeout warning appears before expiry', async ({ page }) => {
    // This test requires JWT time manipulation or a very short session TTL,
    // which is unreliable in CI environments. Skipped by default.
    await page.goto('/');
    await dismissWelcomeIfPresent(page);

    // Would need to:
    // 1. Set a very short session TTL (e.g., 10s) via server config
    // 2. Wait for the timeout warning modal to appear
    // 3. Assert the warning contains "session" text and a "Stay logged in" button
    await expect(page.locator('.session-warning')).toBeVisible({ timeout: 15_000 });
  });
});
