import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: './tests',
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: 1,
  reporter: process.env.CI
    ? [['html', { open: 'never' }], ['github']]
    : [['html', { open: 'on-failure' }]],

  globalSetup: './global-setup.ts',
  globalTeardown: './global-teardown.ts',

  timeout: 60_000,
  expect: { timeout: 10_000 },

  use: {
    baseURL: process.env.SAIRO_URL || 'http://localhost:8888',
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
    video: 'on',
    actionTimeout: 15_000,
    navigationTimeout: 30_000,
  },

  projects: [
    {
      name: 'auth-setup',
      testDir: '.',
      testMatch: /global-auth\.setup\.ts/,
    },
    {
      name: 'chromium',
      use: {
        ...devices['Desktop Chrome'],
        storageState: './auth-state.json',
      },
      testIgnore: [/01-auth\.spec/, /19-share-links\.spec/],
      dependencies: ['auth-setup'],
    },
    {
      name: 'no-auth',
      use: { ...devices['Desktop Chrome'] },
      testMatch: [/01-auth\.spec/, /19-share-links\.spec/],
    },
  ],
});
