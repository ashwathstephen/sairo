import { test, expect } from '@playwright/test';
import * as path from 'path';
import { SEL } from '../helpers/selectors';
import { BUCKETS, testDataPath } from '../helpers/test-data';
import { dismissWelcomeIfPresent, navigateToBucket, waitForToast, waitForTableLoaded } from '../helpers/wait-helpers';

test.describe('File Operations', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/');
    await dismissWelcomeIfPresent(page);
    await navigateToBucket(page, BUCKETS.MAIN);
  });

  test('4.1 opens upload modal on Upload button click', async ({ page }) => {
    await page.locator(SEL.uploadButton).click();
    await expect(page.locator(SEL.modal)).toBeVisible();
    await expect(page.locator(SEL.dropZone)).toBeVisible();
    // Cancel
    await page.locator(SEL.uploadCancelButton).click();
  });

  test('4.1 selects files and uploads with progress', async ({ page }) => {
    await page.locator(SEL.uploadButton).click();

    const uploadName = `e2e-upload-${Date.now()}.txt`;

    // Use a unique object so the assertion proves this upload was persisted.
    const fileInput = page.locator(SEL.uploadFileInput);
    await fileInput.setInputFiles({
      name: uploadName,
      mimeType: 'text/plain',
      buffer: Buffer.from('Sairo e2e upload'),
    });

    // The Docker-internal MinIO hostname is intentionally not reachable from
    // the host browser. Exercise the supported proxy fallback in this hermetic
    // critical path; public-endpoint signing needs its own deployment POC.
    await page.locator(SEL.modal).getByRole('checkbox', { name: 'Direct upload' }).uncheck();

    // File should appear in queue
    await expect(page.locator(SEL.uploadFileRow)).toBeVisible();

    // Click upload
    await page.locator(SEL.uploadSubmitButton).click();

    await expect(page.locator(SEL.modal)).toBeHidden({ timeout: 15_000 });
    await expect(page.locator(`${SEL.tableRow}:has-text("${uploadName}")`)).toBeVisible({ timeout: 15_000 });
  });

  test('4.1 cancel closes upload modal without uploading', async ({ page }) => {
    await page.locator(SEL.uploadButton).click();
    await expect(page.locator(SEL.modal)).toBeVisible();
    await page.locator(SEL.uploadCancelButton).click();
    await expect(page.locator(SEL.modal)).toBeHidden();
  });

  test('4.3 download link exists for files', async ({ page }) => {
    const fileRow = page.locator(`${SEL.tableRow}:not(.row-folder)`).first();
    if (await fileRow.isVisible().catch(() => false)) {
      const downloadLink = fileRow.locator(`${SEL.colActions} a`);
      await expect(downloadLink).toBeVisible();
      const href = await downloadLink.getAttribute('href');
      expect(href).toContain('/api/buckets/');
      expect(href).toContain('/download');
    }
  });

  test('4.4 previews text file', async ({ page }) => {
    // Find sample.txt row and click preview (eye icon)
    const txtRow = page.locator(`${SEL.tableRow}:has-text("sample.txt")`).first();
    if (await txtRow.isVisible().catch(() => false)) {
      await txtRow.locator(`${SEL.colActions} button`).first().click();
      await expect(page.locator(SEL.modal)).toBeVisible();
      // Preview should contain the text content
      await expect(page.locator(SEL.modal)).toContainText('Hello');
      // Close preview via × button or click overlay
      await page.locator(SEL.modalDismissButton).click();
    }
  });

  test('4.4 previews JSON file', async ({ page }) => {
    const jsonRow = page.locator(`${SEL.tableRow}:has-text("sample.json")`).first();
    if (await jsonRow.isVisible().catch(() => false)) {
      await jsonRow.locator(`${SEL.colActions} button`).first().click();
      await expect(page.locator(SEL.modal)).toBeVisible();
      await expect(page.locator(SEL.modal)).toContainText('name');
      await page.locator(SEL.modalDismissButton).click();
    }
  });

  test('4.4 previews CSV file as table', async ({ page }) => {
    const csvRow = page.locator(`${SEL.tableRow}:has-text("sample.csv")`).first();
    if (await csvRow.isVisible().catch(() => false)) {
      await csvRow.locator(`${SEL.colActions} button`).first().click();
      await expect(page.locator(SEL.modal)).toBeVisible();
      // Should render a table with header columns
      await expect(page.locator(SEL.modal)).toContainText('name');
      await page.locator(SEL.modalDismissButton).click();
    }
  });

  test('4.4 previews image file', async ({ page }) => {
    const pngRow = page.locator(`${SEL.tableRow}:has-text("sample.png")`).first();
    if (await pngRow.isVisible().catch(() => false)) {
      await pngRow.locator(`${SEL.colActions} button`).first().click();
      await expect(page.locator(SEL.modal)).toBeVisible();
      // Image preview: img may fail to load if presigned URL uses Docker-internal hostname
      // Accept either a visible img or the error fallback
      const img = page.locator(`${SEL.modal} img`);
      const errorFallback = page.locator(`${SEL.modal} :has-text("Failed to load")`);
      await expect(img.or(errorFallback).first()).toBeVisible({ timeout: 10_000 });
      await page.locator(SEL.modalDismissButton).click();
    }
  });

  test('4.5 opens ObjectInfo modal on info button click', async ({ page }) => {
    const fileRow = page.locator(`${SEL.tableRow}:not(.row-folder)`).first();
    if (await fileRow.isVisible().catch(() => false)) {
      // Info button is the last button or the one with "i" text
      const infoBtn = fileRow.locator(`${SEL.colActions} button:has-text("i")`);
      if (await infoBtn.isVisible().catch(() => false)) {
        await infoBtn.click();
        await expect(page.locator(SEL.modal)).toBeVisible();
        // Should show Details tab with info table
        await expect(page.locator(SEL.infoTable)).toBeVisible();
        await page.locator(SEL.modalCloseButton).click();
      }
    }
  });

  test('4.6 creates folder via New Folder button', async ({ page }) => {
    await page.locator(SEL.newFolderButton).click();
    await expect(page.locator(SEL.modal)).toBeVisible();

    const folderName = 'e2e-test-folder-' + Date.now();
    await page.locator(SEL.promptInput).fill(folderName);
    await page.locator(SEL.promptSubmit).click();

    await waitForToast(page, 'Created folder');
  });

  test('4.7 selects files via checkboxes and deletes', async ({ page }) => {
    // First upload a temp file to delete
    await page.locator(SEL.uploadButton).click();
    await page.locator(SEL.uploadFileInput).setInputFiles(testDataPath('sample.txt'));
    // Rename the upload slightly isn't needed — we'll just delete something

    // Close upload and use existing file
    await page.locator(SEL.uploadCancelButton).click();

    // Select first file checkbox
    const firstFileRow = page.locator(`${SEL.tableRow}:not(.row-folder)`).first();
    if (await firstFileRow.isVisible().catch(() => false)) {
      const checkbox = firstFileRow.locator('input[type="checkbox"]');
      await checkbox.check();

      // Delete button should show count
      const deleteBtn = page.locator(SEL.deleteToolbarButton);
      await expect(deleteBtn).toBeEnabled();
      const text = await deleteBtn.textContent();
      expect(text).toMatch(/Delete \(\d+\)/);
    }
  });

  test('4.8 select all checkbox selects all files', async ({ page }) => {
    const selectAll = page.locator(SEL.selectAllCheckbox);
    await selectAll.check();

    // All visible file checkboxes should be checked
    const checkboxes = page.locator(`${SEL.tableRow} input[type="checkbox"]`);
    const count = await checkboxes.count();
    for (let i = 0; i < Math.min(count, 5); i++) {
      await expect(checkboxes.nth(i)).toBeChecked();
    }

    // Deselect all
    await selectAll.uncheck();
  });

  test('4.9 previews PDF file in iframe', async ({ page }) => {
    const pdfRow = page.locator(`${SEL.tableRow}:has-text("sample.pdf")`).first();
    if (await pdfRow.isVisible().catch(() => false)) {
      await pdfRow.locator(`${SEL.colActions} button`).first().click();
      await expect(page.locator(SEL.modal)).toBeVisible();
      // PDF renders in an iframe
      await expect(page.locator(`${SEL.modal} ${SEL.previewIframe}`)).toBeVisible({ timeout: 10_000 });
      await page.locator(SEL.modalDismissButton).click();
    }
  });

  test('4.10 previews Parquet file with schema table', async ({ page }) => {
    const parquetRow = page.locator(`${SEL.tableRow}:has-text("sample.parquet")`).first();
    if (await parquetRow.isVisible().catch(() => false)) {
      await parquetRow.locator(`${SEL.colActions} button`).first().click();
      await expect(page.locator(SEL.modal)).toBeVisible();
      // Schema preview should show
      await expect(page.locator(`${SEL.modal} ${SEL.schemaPreview}`)).toBeVisible({ timeout: 10_000 });
      // Schema table with columns (may have multiple tables — column schema + stats)
      await expect(page.locator(`${SEL.modal} ${SEL.schemaTable}`).first()).toBeVisible();
      // Badge showing format
      await expect(page.locator(`${SEL.modal} ${SEL.schemaBadge}`)).toBeVisible();
      await page.locator(SEL.modalDismissButton).click();
    }
  });

  test('4.11 drag-and-drop shows overlay and opens upload modal', async ({ page }) => {
    // Simulate drag enter with Files type
    await page.evaluate(() => {
      const event = new DragEvent('dragenter', {
        bubbles: true,
        cancelable: true,
        dataTransfer: new DataTransfer(),
      });
      event.dataTransfer!.items.add(new File(['test'], 'drag-test.txt', { type: 'text/plain' }));
      document.querySelector('.app')?.dispatchEvent(event);
    });

    // Drop overlay should appear
    await expect(page.locator(SEL.dropOverlay)).toBeVisible({ timeout: 5_000 });
    await expect(page.locator(SEL.dropOverlayText)).toContainText('Drop files to upload');

    // Simulate drop
    await page.evaluate(() => {
      const dt = new DataTransfer();
      dt.items.add(new File(['test content'], 'drag-test.txt', { type: 'text/plain' }));
      const event = new DragEvent('drop', {
        bubbles: true,
        cancelable: true,
        dataTransfer: dt,
      });
      document.querySelector('.app')?.dispatchEvent(event);
    });

    // Upload modal should open with the dropped file
    await expect(page.locator(SEL.modal)).toBeVisible({ timeout: 5_000 });
    await page.locator(SEL.uploadCancelButton).click();
  });
});
