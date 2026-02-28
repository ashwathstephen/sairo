import { test, expect } from '@playwright/test';
import { SEL } from '../helpers/selectors';
import { BUCKETS } from '../helpers/test-data';
import { dismissWelcomeIfPresent, waitForToast } from '../helpers/wait-helpers';

test.describe('Bucket List (Home Page)', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/');
    await dismissWelcomeIfPresent(page);
    await expect(page.locator(SEL.bucketCard).first()).toBeVisible({ timeout: 15_000 });
  });

  test('2.1 displays all test buckets as cards', async ({ page }) => {
    for (const bucket of BUCKETS.ALL) {
      await expect(page.locator(`${SEL.bucketCard}:has-text("${bucket}")`)).toBeVisible();
    }
  });

  test('2.1 shows bucket count in toolbar', async ({ page }) => {
    const countText = await page.locator(SEL.bucketCount).textContent();
    expect(countText).toMatch(/\d+ bucket/);
  });

  test('2.1 each card shows bucket name', async ({ page }) => {
    const firstCard = page.locator(SEL.bucketCard).first();
    await expect(firstCard.locator(SEL.bucketCardName)).toBeVisible();
  });

  test('2.2 shows inline form when clicking + Create Bucket', async ({ page }) => {
    await page.locator(SEL.createBucketBtn).click();
    await expect(page.locator(SEL.createBucketInput)).toBeVisible();
  });

  test('2.2 creates bucket by pressing Enter', async ({ page }) => {
    const testBucket = 'e2e-create-enter-test';
    await page.locator(SEL.createBucketBtn).click();
    await page.locator(SEL.createBucketInput).fill(testBucket);
    await page.locator(SEL.createBucketInput).press('Enter');
    await waitForToast(page, testBucket);
    await expect(page.locator(`${SEL.bucketCard}:has-text("${testBucket}")`)).toBeVisible();

    // Cleanup: delete the bucket
    await page.locator(`${SEL.bucketCard}:has-text("${testBucket}") ${SEL.bucketDeleteBtn}`).click();
    await page.locator(SEL.deleteConfirmButton).click();
  });

  test('2.2 creates bucket by clicking Create button', async ({ page }) => {
    const testBucket = 'e2e-create-click-test';
    await page.locator(SEL.createBucketBtn).click();
    await page.locator(SEL.createBucketInput).fill(testBucket);
    await page.locator(SEL.createBucketSubmit).click();
    await waitForToast(page, testBucket);
    await expect(page.locator(`${SEL.bucketCard}:has-text("${testBucket}")`)).toBeVisible();

    // Cleanup
    await page.locator(`${SEL.bucketCard}:has-text("${testBucket}") ${SEL.bucketDeleteBtn}`).click();
    await page.locator(SEL.deleteConfirmButton).click();
  });

  test('2.2 invalid bucket name does not create bucket', async ({ page }) => {
    await page.locator(SEL.createBucketBtn).click();
    await page.locator(SEL.createBucketInput).fill('INVALID-UPPER');
    await page.locator(SEL.createBucketInput).press('Enter');

    // Wait for the create attempt to finish (button text goes Creating... → Create)
    await expect(page.locator(SEL.createBucketSubmit)).toContainText('Create', { timeout: 10_000 });

    // The bucket should NOT appear in the list
    await expect(page.locator(`${SEL.bucketCard}:has-text("INVALID-UPPER")`)).toBeHidden();

    // Form stays open — cancel it
    await page.locator(SEL.createBucketCancel).click();
  });

  test('2.2 closes form when clicking Cancel', async ({ page }) => {
    await page.locator(SEL.createBucketBtn).click();
    await expect(page.locator(SEL.createBucketInput)).toBeVisible();
    await page.locator(SEL.createBucketCancel).click();
    await expect(page.locator(SEL.createBucketInput)).toBeHidden();
  });

  test('2.3 shows confirmation dialog on delete click', async ({ page }) => {
    const card = page.locator(`${SEL.bucketCard}:has-text("${BUCKETS.EMPTY}")`);
    await card.hover();
    await card.locator(SEL.bucketDeleteBtn).click();
    await expect(page.locator(SEL.modal)).toBeVisible();
    // Cancel
    await page.locator(SEL.deleteCancelButton).click();
  });

  test('2.3 shows error when deleting non-empty bucket', async ({ page }) => {
    const card = page.locator(`${SEL.bucketCard}:has-text("${BUCKETS.MAIN}")`);
    await card.hover();
    await card.locator(SEL.bucketDeleteBtn).click();
    await page.locator(SEL.deleteConfirmButton).click();
    await waitForToast(page, '', 'error');
  });

  test('2.4 navigates into bucket on card click', async ({ page }) => {
    await page.locator(`${SEL.bucketCard}:has-text("${BUCKETS.MAIN}")`).click();
    // URL should update to include bucket name
    await expect(page).toHaveURL(new RegExp(BUCKETS.MAIN));
    // Should see breadcrumb or table
    await expect(page.locator(SEL.breadcrumb)).toBeVisible();
  });
});
