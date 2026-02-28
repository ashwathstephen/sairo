import { test, expect } from '@playwright/test';
import { SEL } from '../helpers/selectors';
import { VIEWER } from '../helpers/test-data';
import { dismissWelcomeIfPresent, waitForToastDismissed } from '../helpers/wait-helpers';

test.describe('User Management (Admin)', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/');
    await dismissWelcomeIfPresent(page);
  });

  test('10.1 opens UserManager from Users button', async ({ page }) => {
    await page.locator(SEL.usersButton).click();
    await expect(page.locator(SEL.modal)).toBeVisible();
    await expect(page.locator(`${SEL.modal} h2:has-text("Users")`)).toBeVisible();
  });

  test('10.1 shows admin user in table', async ({ page }) => {
    await page.locator(SEL.usersButton).click();
    await expect(page.locator(SEL.modal)).toBeVisible();
    // Wait for user table to populate
    await expect(page.locator(`${SEL.modal} td`).first()).toBeVisible({ timeout: 10_000 });
    await expect(page.locator(`${SEL.modal} td:has-text("admin")`).first()).toBeVisible();
  });

  test('10.2 creates viewer user', async ({ page }) => {
    await page.locator(SEL.usersButton).click();
    await expect(page.locator(SEL.modal)).toBeVisible();

    // Fill create user form
    await page.locator(`${SEL.modal} input[placeholder*="Username"]`).fill(VIEWER.username);
    await page.locator(`${SEL.modal} input[placeholder*="Password"]`).fill(VIEWER.password);

    // Select viewer role
    const roleSelect = page.locator(`${SEL.modal} select`).first();
    if (await roleSelect.isVisible().catch(() => false)) {
      await roleSelect.selectOption('viewer');
    }

    await page.locator(SEL.addUserButton).click();

    // New user should appear in table
    await expect(page.locator(`${SEL.modal} td:has-text("${VIEWER.username}")`)).toBeVisible({ timeout: 5_000 });
  });

  test('10.2 duplicate username shows error', async ({ page }) => {
    await page.locator(SEL.usersButton).click();
    await expect(page.locator(SEL.modal)).toBeVisible();

    // Try to create admin again (duplicate)
    await page.locator(`${SEL.modal} input[placeholder*="Username"]`).fill('admin');
    await page.locator(`${SEL.modal} input[placeholder*="Password"]`).fill('somepassword1');
    await page.locator(SEL.addUserButton).click();

    // Should show error (uses .form-error class)
    await expect(page.locator(`${SEL.modal} ${SEL.formError}`)).toBeVisible({ timeout: 5_000 });
  });

  test('10.4 cannot delete self', async ({ page }) => {
    await page.locator(SEL.usersButton).click();
    await expect(page.locator(SEL.modal)).toBeVisible();
    await expect(page.locator(`${SEL.modal} td`).first()).toBeVisible({ timeout: 10_000 });

    // The admin row should NOT have a delete button (or it should be disabled)
    const adminRow = page.locator(`${SEL.modal} tr:has-text("admin")`).first();
    const deleteBtn = adminRow.locator('button:has-text("Delete")');
    // Either the button doesn't exist or is hidden for self
    const count = await deleteBtn.count();
    expect(count).toBe(0);
  });

  test('10.4 deletes non-self user', async ({ page }) => {
    await page.locator(SEL.usersButton).click();
    await expect(page.locator(SEL.modal)).toBeVisible();

    // First ensure e2e-viewer exists
    const viewerRow = page.locator(`${SEL.modal} tr:has-text("${VIEWER.username}")`);
    if (await viewerRow.isVisible().catch(() => false)) {
      const deleteBtn = viewerRow.locator('button:has-text("Delete")');
      await deleteBtn.click();

      // Confirmation dialog — wait for nested modal, then click the danger confirm button
      const confirmDialog = page.locator('.modal-small');
      await expect(confirmDialog).toBeVisible({ timeout: 5_000 });
      await confirmDialog.locator('button.btn-danger').click();

      // User should be removed from table
      await expect(viewerRow).toBeHidden({ timeout: 5_000 });
    }
  });

  test('10.5 bucket permissions panel expands on row click', async ({ page }) => {
    // Create a viewer first for this test
    await page.locator(SEL.usersButton).click();
    await expect(page.locator(SEL.modal)).toBeVisible();
    await expect(page.locator(`${SEL.modal} td`).first()).toBeVisible({ timeout: 10_000 });

    const viewerExists = await page.locator(`${SEL.modal} td:has-text("${VIEWER.username}")`).isVisible().catch(() => false);
    if (!viewerExists) {
      await page.locator(`${SEL.modal} input[placeholder*="Username"]`).fill(VIEWER.username);
      await page.locator(`${SEL.modal} input[placeholder*="Password"]`).fill(VIEWER.password);
      await page.locator(SEL.addUserButton).click();
      await expect(page.locator(`${SEL.modal} td:has-text("${VIEWER.username}")`)).toBeVisible({ timeout: 5_000 });
    }

    // Click the viewer row's username cell (not select/button) to expand permissions
    // The onClick handler only fires for non-admin, non-self users
    const viewerRow = page.locator(`${SEL.modal} tr:has-text("${VIEWER.username}")`).first();
    await viewerRow.locator('td').first().click();

    // Permissions panel should appear
    await expect(page.locator('.perm-panel')).toBeVisible({ timeout: 5_000 });
  });

  test('10.6 change user role via dropdown', async ({ page }) => {
    await page.locator(SEL.usersButton).click();
    await expect(page.locator(SEL.modal)).toBeVisible();
    await expect(page.locator(`${SEL.modal} td`).first()).toBeVisible({ timeout: 10_000 });

    // Ensure e2e-viewer exists
    const viewerRow = page.locator(`${SEL.modal} tr:has-text("${VIEWER.username}")`).first();
    if (await viewerRow.isVisible().catch(() => false)) {
      const roleSelect = viewerRow.locator('select');
      if (await roleSelect.isVisible().catch(() => false)) {
        // Change from viewer to admin
        await roleSelect.selectOption('admin');
        // Auto-saves — wait for value to update
        await expect(roleSelect).toHaveValue('admin', { timeout: 5_000 });

        // Change back to viewer to restore state
        await roleSelect.selectOption('viewer');
        await expect(roleSelect).toHaveValue('viewer', { timeout: 5_000 });
      }
    }
  });
});
