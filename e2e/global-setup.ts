import { FullConfig } from '@playwright/test';
import { execSync } from 'child_process';
import { ApiClient } from './helpers/api-client';
import { BUCKETS, TEST_FILES } from './helpers/test-data';

async function globalSetup(config: FullConfig) {
  const baseURL = process.env.SAIRO_URL || 'http://localhost:8888';

  // 1. Start Docker Compose unless told not to
  if (process.env.START_DOCKER !== 'false') {
    console.log('Starting test infrastructure (MinIO + Sairo)...');
    try {
      execSync('docker compose -f e2e/docker-compose.test.yml up -d --build --wait', {
        cwd: process.cwd().includes('/e2e') ? process.cwd().replace('/e2e', '') : process.cwd(),
        stdio: 'inherit',
        timeout: 300_000,
      });
    } catch (err) {
      // If --wait isn't supported, fall back to just up -d
      execSync('docker compose -f e2e/docker-compose.test.yml up -d --build', {
        cwd: process.cwd().includes('/e2e') ? process.cwd().replace('/e2e', '') : process.cwd(),
        stdio: 'inherit',
        timeout: 300_000,
      });
    }
  }

  // 2. Wait for Sairo to be healthy
  console.log('Waiting for Sairo to be healthy...');
  const api = new ApiClient(baseURL);
  await api.waitForHealthy(90_000);
  console.log('Sairo is healthy.');

  // 3. Login as admin
  await api.login('admin', 'password');

  // 4. Create test buckets
  console.log('Creating test buckets...');
  for (const bucket of BUCKETS.ALL) {
    await api.createBucket(bucket).catch((err) => {
      console.warn(`  Bucket ${bucket}: ${err.message}`);
    });
  }

  // 5. Upload seed test files into the main bucket
  console.log('Uploading test files...');
  for (const file of TEST_FILES) {
    await api.uploadFile(BUCKETS.MAIN, file.prefix, file.name).catch((err) => {
      console.warn(`  Upload ${file.name} to ${file.prefix}: ${err.message}`);
    });
  }

  // Upload a file to the settings bucket for settings mutation tests
  await api.uploadFile(BUCKETS.SETTINGS, '', 'sample.txt').catch(() => {});

  // Also upload a file to the versioned bucket for version tests
  await api.uploadFile(BUCKETS.VERSIONED, '', 'sample.txt').catch(() => {});

  // 6. Enable versioning on the versioned bucket
  await api.enableVersioning(BUCKETS.VERSIONED);

  // Upload a second version of sample.txt to create version history
  await api.uploadFile(BUCKETS.VERSIONED, '', 'sample.txt').catch(() => {});

  // 7. Trigger indexing and wait
  console.log('Waiting for indexing...');
  await api.triggerCrawl(BUCKETS.MAIN);
  await api.waitForIndex(BUCKETS.MAIN, 60_000);

  console.log('Global setup complete.');
}

export default globalSetup;
