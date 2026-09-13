import { test, expect } from '@playwright/test';
import { SEL } from '../helpers/selectors';
import { BUCKETS } from '../helpers/test-data';
import { navigateToBucket, clickTab, openFileInfo } from '../helpers/wait-helpers';

// Issue #28: a per-bucket public base URL (CDN) gives every object a ready-to-copy public link.
test.describe('Public URL per bucket', () => {
  test.describe.configure({ mode: 'serial' });
  const BASE = 'https://cdn.example.com/files';

  async function setPublicBase(page, value: string) {
    await navigateToBucket(page, BUCKETS.MAIN);
    await page.locator(SEL.settingsButton).click();
    await expect(page.locator(SEL.modal)).toBeVisible();
    await clickTab(page, 'Tags');
    await page.locator('input[aria-label="Public URL base"]').fill(value);
    await page.locator(`${SEL.modal} button:has-text("Save")`).first().click();
    await expect(page.locator(`${SEL.modal} :text("Saved")`)).toBeVisible();
    await page.locator('.modal-overlay').click({ position: { x: 5, y: 5 } });   // overlay click closes the dialog
    await expect(page.locator(SEL.modal)).toBeHidden();
  }

  test('28.1 setting the base shows a public link in the object dialog', async ({ page }) => {
    await setPublicBase(page, BASE);
    await openFileInfo(page, BUCKETS.MAIN, 'sample.txt');
    await clickTab(page, 'Share');
    const link = page.locator('input[aria-label="Public link"]');
    await expect(link).toBeVisible();
    const value = await link.inputValue();
    expect(value.startsWith(BASE + '/')).toBeTruthy();
    expect(value.length).toBeGreaterThan(BASE.length + 1);
  });

  test('28.2 clearing the base removes the public link', async ({ page }) => {
    await setPublicBase(page, '');
    await openFileInfo(page, BUCKETS.MAIN, 'sample.txt');
    await clickTab(page, 'Share');
    await expect(page.locator('input[aria-label="Public link"]')).toHaveCount(0);
  });
});
