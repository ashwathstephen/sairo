/**
 * Feature showcase video — single continuous test that walks through
 * key Sairo features. Playwright records the browser tab automatically.
 *
 * Run:   npm run demo-video
 * Output: docs/demo.gif (+ docs/demo.webm intermediate)
 */
import { test, expect } from '@playwright/test';
import * as path from 'path';
import { execSync } from 'child_process';
import { SEL } from '../helpers/selectors';
import { BUCKETS } from '../helpers/test-data';
import {
  dismissWelcomeIfPresent,
  navigateToBucket,
  waitForTableLoaded,
  waitForModalClosed,
} from '../helpers/wait-helpers';

const DOCS_DIR = path.resolve(__dirname, '..', '..', 'docs');
const WEBM_PATH = path.join(DOCS_DIR, 'demo.webm');
const GIF_PATH = path.join(DOCS_DIR, 'demo.gif');

// Deliberate pause so viewers can see each state
const pause = (ms = 1500) => new Promise((r) => setTimeout(r, ms));

// video config must be top-level (not inside describe)
test.use({
  viewport: { width: 1440, height: 900 },
  video: { mode: 'on', size: { width: 1440, height: 900 } },
});

test('Feature walkthrough', async ({ page }, testInfo) => {
  // Increase timeout for this long demo
  test.setTimeout(120_000);

  // ── 1. Bucket list (light mode) ──
  await page.goto('/');
  await dismissWelcomeIfPresent(page);
  await expect(page.locator(SEL.bucketCard).first()).toBeVisible({ timeout: 15_000 });

  // Ensure light mode to start
  const theme = await page.evaluate(() => document.documentElement.getAttribute('data-theme'));
  if (theme === 'dark') {
    await page.locator(SEL.themeToggle).click();
    await pause(500);
  }
  await pause(2000);

  // ── 2. Navigate into bucket ──
  await navigateToBucket(page, BUCKETS.MAIN);
  await expect(page.locator(SEL.tableRow).first()).toBeVisible();
  await pause(2000);

  // ── 3. Navigate into a folder ──
  const folderLink = page.locator(SEL.folderLink).first();
  if (await folderLink.isVisible().catch(() => false)) {
    await folderLink.click();
    await waitForTableLoaded(page);
    await pause(1500);

    // Go back via breadcrumb
    await page.locator(`${SEL.breadcrumb} >> text=${BUCKETS.MAIN}`).click();
    await waitForTableLoaded(page);
    await pause(1000);
  }

  // ── 4. Filter by name ──
  const filterInput = page.locator(SEL.filterInput);
  await filterInput.click();
  await page.keyboard.type('json', { delay: 100 });
  await pause(1500);
  await filterInput.fill('');
  await pause(500);

  // ── 5. Search ──
  await page.locator(SEL.searchButton).click();
  await expect(page.locator(SEL.searchInput)).toBeVisible();
  await page.locator(SEL.searchInput).type('sample', { delay: 80 });
  await expect(
    page.locator(`${SEL.searchItem}, ${SEL.searchCount}`).first()
  ).toBeVisible({ timeout: 10_000 });
  await pause(2500);

  // Close search
  await page.keyboard.press('Escape');
  await pause(500);

  // ── 6. Object details ──
  const fileRow = page.locator(`${SEL.tableRow}:has-text("sample.json")`);
  await expect(fileRow).toBeVisible();
  await fileRow.locator(`${SEL.colActions} button:has-text("i")`).click();
  await expect(page.locator(SEL.modal)).toBeVisible();
  await expect(page.locator(SEL.infoTable)).toBeVisible({ timeout: 10_000 });
  await pause(2000);

  // Show Tags tab
  await page.locator(`${SEL.tabButton}:has-text("Tags")`).click();
  await pause(1500);

  // Show Share tab
  await page.locator(`${SEL.tabButton}:has-text("Share")`).click();
  await pause(1500);

  // Close modal
  await page.locator(SEL.modalCloseButton).click();
  await waitForModalClosed(page);
  await pause(500);

  // ── 7. Storage dashboard ──
  await page.locator(SEL.dashboardButton).click();
  await expect(page.locator(SEL.dashboardModal)).toBeVisible();
  await expect(page.locator(SEL.dashboardCard).first()).toBeVisible({ timeout: 10_000 });
  await expect(page.locator(SEL.dashboardBarRow).first()).toBeVisible({ timeout: 10_000 });
  await pause(2500);

  // Close dashboard
  await page.locator(SEL.modalCloseButton).click();
  await waitForModalClosed(page);
  await pause(500);

  // ── 8. Switch to dark mode ──
  await page.locator(SEL.themeToggle).click();
  await pause(2000);

  // ── 9. Browse in dark mode ──
  await pause(1500);

  // ── 10. Go back home to show dark bucket list ──
  await page.locator(SEL.headerTitle).click();
  await expect(page.locator(SEL.bucketCard).first()).toBeVisible({ timeout: 15_000 });
  await pause(2000);

  // ── 11. Switch back to light to end clean ──
  await page.locator(SEL.themeToggle).click();
  await pause(1500);

  // Save video and convert to GIF
  const video = page.video();
  if (video) {
    await page.close();
    await video.saveAs(WEBM_PATH);
    console.log(`Demo video saved to ${WEBM_PATH}`);

    // Convert to GIF using ffmpeg
    try {
      execSync(
        `ffmpeg -y -i "${WEBM_PATH}" -vf "fps=12,scale=960:-1:flags=lanczos,split[s0][s1];[s0]palettegen=max_colors=128:stats_mode=diff[p];[s1][p]paletteuse=dither=bayer:bayer_scale=3" -loop 0 "${GIF_PATH}"`,
        { stdio: 'pipe' }
      );
      console.log(`Demo GIF saved to ${GIF_PATH}`);
    } catch (e) {
      console.warn('ffmpeg not available — skipping GIF conversion. Install ffmpeg and re-run.');
    }
  }
});
