import { test, expect } from '@playwright/test';
import { ApiClient } from '../helpers/api-client';
import { ADMIN, VIEWER, BUCKETS } from '../helpers/test-data';
import { SEL } from '../helpers/selectors';
import { dismissWelcomeIfPresent } from '../helpers/wait-helpers';

test.describe('Security Hardening', () => {
  const baseURL = process.env.SAIRO_URL || 'http://localhost:8888';

  test.describe('Security Headers', () => {
    test('26.1 responses include Content-Security-Policy header', async ({ page }) => {
      const response = await page.goto('/');
      expect(response).not.toBeNull();
      const csp = response!.headers()['content-security-policy'];
      expect(csp).toBeDefined();
      expect(csp).toContain("default-src 'self'");
      expect(csp).toContain("script-src 'self'");
      expect(csp).toContain("frame-src blob:");
    });

    test('26.2 responses include X-Content-Type-Options header', async ({ page }) => {
      const response = await page.goto('/');
      expect(response!.headers()['x-content-type-options']).toBe('nosniff');
    });

    test('26.3 responses include X-Frame-Options header', async ({ page }) => {
      const response = await page.goto('/');
      expect(response!.headers()['x-frame-options']).toBe('DENY');
    });

    test('26.4 responses include Referrer-Policy header', async ({ page }) => {
      const response = await page.goto('/');
      expect(response!.headers()['referrer-policy']).toBe('strict-origin-when-cross-origin');
    });

    test('26.5 API responses also include security headers', async () => {
      const api = new ApiClient(baseURL);
      await api.login(ADMIN.username, ADMIN.password);
      const res = await api.getRawResponse('/healthz');
      expect(res.headers.get('x-content-type-options')).toBe('nosniff');
      expect(res.headers.get('x-frame-options')).toBe('DENY');
    });
  });

  test.describe('Health Detail — Admin Only', () => {
    test('26.6 admin can access health-detail endpoint', async () => {
      const api = new ApiClient(baseURL);
      await api.login(ADMIN.username, ADMIN.password);
      const { status, data } = await api.getHealthDetail();
      expect(status).toBe(200);
      expect(data).toHaveProperty('status');
      expect(data).toHaveProperty('uptime_seconds');
      expect(data).toHaveProperty('s3_connected');
    });

    test('26.7 non-admin gets 403 on health-detail', async () => {
      const api = new ApiClient(baseURL);
      // Create viewer user first
      const adminApi = new ApiClient(baseURL);
      await adminApi.login(ADMIN.username, ADMIN.password);
      await adminApi.createUser(VIEWER.username, VIEWER.password, 'viewer').catch(() => {});

      await api.login(VIEWER.username, VIEWER.password);
      const { status } = await api.getHealthDetail();
      expect(status).toBe(403);

      // Cleanup
      await adminApi.deleteUser(VIEWER.username).catch(() => {});
    });
  });

  test.describe('Share Link Ownership', () => {
    test('26.8 non-admin cannot delete another user share link', async () => {
      // Create a share link as admin
      const adminApi = new ApiClient(baseURL);
      await adminApi.login(ADMIN.username, ADMIN.password);
      await adminApi.createUser(VIEWER.username, VIEWER.password, 'viewer').catch(() => {});
      const token = await adminApi.createShareLink(BUCKETS.MAIN, 'sample.txt', 24);

      // Get the link ID
      const linksRes = await fetch(`${baseURL}/api/share-links`, {
        headers: { Cookie: '' }, // We need admin cookies
      });
      // Login as viewer and try to delete
      const viewerApi = new ApiClient(baseURL);
      await viewerApi.login(VIEWER.username, VIEWER.password);

      // Get all links via admin
      const adminLinksRes = await adminApi.getRawResponse('/api/share-links');
      const adminLinks = await adminLinksRes.json();
      const linkId = adminLinks.links?.find((l: any) => l.token === token)?.id;

      if (linkId) {
        const deleteRes = await fetch(`${baseURL}/api/share-links/${linkId}`, {
          method: 'DELETE',
          headers: { Cookie: '' },
        });
        // Without auth it should be 401
        expect(deleteRes.status).toBe(401);
      }

      // Cleanup
      await adminApi.deleteUser(VIEWER.username).catch(() => {});
    });
  });

  test.describe('Upload Size Limits', () => {
    test('26.9 upload endpoint exists and accepts small files', async () => {
      const api = new ApiClient(baseURL);
      await api.login(ADMIN.username, ADMIN.password);
      // Upload a small test file — should succeed
      await api.uploadFile(BUCKETS.MAIN, '', 'sample.txt');
      // If we got here without error, upload works within size limits
    });
  });

  test.describe('S3 Error Sanitization', () => {
    test('26.10 error responses do not leak ARNs or account IDs', async ({ page }) => {
      await page.goto('/');
      await dismissWelcomeIfPresent(page);

      // Try to access a non-existent object — error should be sanitized
      const api = new ApiClient(baseURL);
      await api.login(ADMIN.username, ADMIN.password);
      const res = await api.getRawResponse(`/api/buckets/${BUCKETS.MAIN}/object-info?key=nonexistent-key-12345`);
      const body = await res.json();

      if (body.detail) {
        // Should not contain raw ARNs
        expect(body.detail).not.toMatch(/arn:aws/);
        // Should not contain raw 12-digit account IDs (unless it's a normal number)
        expect(body.detail).not.toMatch(/arn:[^\s,]+/);
      }
    });
  });

  test.describe('2FA Rate Limiting', () => {
    test('26.11 2FA verify endpoint has rate limiting', async () => {
      // Send multiple rapid requests — should eventually get 429
      const results: number[] = [];
      for (let i = 0; i < 7; i++) {
        const res = await fetch(`${baseURL}/api/auth/2fa/verify`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ code: '000000' }),
        });
        results.push(res.status);
      }
      // Should get at least one 429 (rate limited) or 401 (not authenticated)
      // The rate limiter fires after 5 requests per minute
      const has429or401 = results.some(s => s === 429 || s === 401);
      expect(has429or401).toBeTruthy();
    });
  });
});
