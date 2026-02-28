import { Page, expect } from '@playwright/test';
import { SEL } from './selectors';

/** Wait for a toast notification containing the specified text. */
export async function waitForToast(page: Page, textContains: string, type?: 'success' | 'error' | 'warning') {
  const selector = type ? `.toast-${type}` : '.toast';
  const toast = page.locator(selector).filter({ hasText: textContains });
  await expect(toast.first()).toBeVisible({ timeout: 10_000 });
}

/** Wait for all modal overlays to close. */
export async function waitForModalClosed(page: Page) {
  await expect(page.locator(SEL.modalOverlay)).toBeHidden({ timeout: 10_000 });
}

/** Wait for either table rows or empty state to appear (object list loaded). */
export async function waitForTableLoaded(page: Page) {
  await page.waitForFunction(() => {
    return document.querySelectorAll('.table-row').length > 0
      || document.querySelector('.empty-state') !== null;
  }, { timeout: 15_000 });
}

/** Dismiss the first-visit welcome modal if it appears. */
export async function dismissWelcomeIfPresent(page: Page) {
  const gotIt = page.locator(SEL.welcomeGotIt);
  if (await gotIt.isVisible({ timeout: 2000 }).catch(() => false)) {
    await gotIt.click();
    await expect(gotIt).toBeHidden();
  }
}

/** Navigate from bucket list into a specific bucket by clicking its card. */
export async function navigateToBucket(page: Page, bucketName: string) {
  await dismissWelcomeIfPresent(page);
  await page.locator(`${SEL.bucketCard}:has-text("${bucketName}")`).click();
  await waitForTableLoaded(page);
}

/** Navigate to bucket list home (click app title or go to /). */
export async function goHome(page: Page) {
  await page.goto('/');
  await dismissWelcomeIfPresent(page);
  await expect(page.locator(SEL.bucketCard).first()).toBeVisible({ timeout: 15_000 });
}

/** Login as admin via the UI. */
export async function loginAsAdmin(page: Page) {
  await page.goto('/');
  await page.locator(SEL.usernameInput).fill('admin');
  await page.locator(SEL.passwordInput).fill('password');
  await page.locator(SEL.signInButton).click();
  await expect(page.locator(SEL.bucketCard).first()).toBeVisible({ timeout: 15_000 });
  await dismissWelcomeIfPresent(page);
}

/** Wait for a specific tab to become active. */
export async function clickTab(page: Page, tabName: string) {
  await page.locator(`${SEL.tabButton}:has-text("${tabName}")`).click();
  await expect(page.locator(`${SEL.tabActive}:has-text("${tabName}")`)).toBeVisible();
}

/** Wait for a toast to disappear from the DOM. */
export async function waitForToastDismissed(page: Page, textContains: string, timeoutMs = 8000) {
  const toast = page.locator('.toast').filter({ hasText: textContains });
  await expect(toast).toBeHidden({ timeout: timeoutMs });
}

/** Open ObjectInfo modal for a file — navigate to bucket, click info button. */
export async function openFileInfo(page: Page, bucket: string, filename: string) {
  await navigateToBucket(page, bucket);
  const fileRow = page.locator(`.table-row:has-text("${filename}")`).first();
  await fileRow.locator('.col-actions button.btn-info').click();
  await expect(page.locator(SEL.modal)).toBeVisible();
}

/** Wait for loading spinner / progress to finish. */
export async function waitForLoadingDone(page: Page) {
  await page.waitForFunction(() => {
    return !document.querySelector('.progress-bar') || document.querySelector('.progress-bar')?.childElementCount === 0;
  }, { timeout: 15_000 }).catch(() => {});
}
