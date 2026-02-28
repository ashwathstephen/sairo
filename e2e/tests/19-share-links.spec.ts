import { test, expect } from '@playwright/test';
import { ApiClient } from '../helpers/api-client';
import { BUCKETS, ADMIN } from '../helpers/test-data';

test.describe('Share Links (Public Access)', () => {
  test.use({ storageState: { cookies: [], origins: [] } }); // No auth — tests public access

  let shareToken: string;
  let passwordToken: string;

  test.beforeAll(async () => {
    // Create share links via API
    const baseURL = process.env.SAIRO_URL || 'http://localhost:8888';
    const api = new ApiClient(baseURL);
    await api.login(ADMIN.username, ADMIN.password);

    // Public share link (no password)
    shareToken = await api.createShareLink(BUCKETS.MAIN, 'sample.txt', 24);

    // Password-protected share link
    passwordToken = await api.createShareLink(BUCKETS.MAIN, 'sample.txt', 24, 'testpass123');
  });

  test('19 share link page loads at /share/{token}', async ({ page }) => {
    await page.goto(`/share/${shareToken}`);

    // Should show file details (not login page)
    await expect(page.locator(':has-text("sample.txt")').first()).toBeVisible({ timeout: 10_000 });
  });

  test('19 shows Download button on public share link', async ({ page }) => {
    await page.goto(`/share/${shareToken}`);

    await expect(page.locator('button:has-text("Download"), a:has-text("Download")').first()).toBeVisible({ timeout: 10_000 });
  });

  test('19 password-protected link prompts for password', async ({ page }) => {
    await page.goto(`/share/${passwordToken}`);

    // Should show password input
    await expect(page.locator('input[type="password"]').first()).toBeVisible({ timeout: 10_000 });
  });

  test('19 password-protected link works with correct password', async ({ page }) => {
    await page.goto(`/share/${passwordToken}`);

    await page.locator('input[type="password"]').fill('testpass123');
    await page.locator('button:has-text("Access"), button:has-text("Submit"), button[type="submit"]').first().click();

    // Should show file details after password entry
    await expect(page.locator(':has-text("sample.txt")').first()).toBeVisible({ timeout: 10_000 });
  });

  test('19 invalid token shows error', async ({ page }) => {
    await page.goto('/share/invalid-nonexistent-token-xyz');

    // Should show error or not found message
    await expect(page.locator(':has-text("not found"), :has-text("expired"), :has-text("invalid"), :has-text("error")').first()).toBeVisible({ timeout: 10_000 });
  });
});
