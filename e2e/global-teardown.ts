import { FullConfig } from '@playwright/test';
import { execSync } from 'child_process';
import { ApiClient } from './helpers/api-client';
import { BUCKETS } from './helpers/test-data';

async function globalTeardown(config: FullConfig) {
  const baseURL = process.env.SAIRO_URL || 'http://localhost:8888';

  // Clean up test buckets
  try {
    const api = new ApiClient(baseURL);
    await api.login('admin', 'password');

    for (const bucket of BUCKETS.ALL) {
      await api.emptyBucket(bucket).catch(() => {});
      await api.deleteBucket(bucket).catch(() => {});
    }

    // Clean up any test users
    await api.deleteUser('e2e-viewer').catch(() => {});
    await api.deleteUser('e2e-test-user').catch(() => {});
  } catch {
    // Cleanup is best-effort
  }

  // Stop Docker in CI
  if (process.env.CI) {
    try {
      execSync('docker compose -f e2e/docker-compose.test.yml down -v', {
        cwd: process.cwd().includes('/e2e') ? process.cwd().replace('/e2e', '') : process.cwd(),
        stdio: 'inherit',
      });
    } catch {}
  }
}

export default globalTeardown;
