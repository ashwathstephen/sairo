import * as path from 'path';

export const BUCKETS = {
  MAIN: 'e2e-test-main',
  EMPTY: 'e2e-test-empty',
  VERSIONED: 'e2e-test-versioned',
  DELETE_ME: 'e2e-test-delete-me',
  SETTINGS: 'e2e-test-settings',
  COPY_DEST: 'e2e-test-copy-dest',
  ALL: [
    'e2e-test-main',
    'e2e-test-empty',
    'e2e-test-versioned',
    'e2e-test-delete-me',
    'e2e-test-settings',
    'e2e-test-copy-dest',
  ],
} as const;

export const TEST_FILES = [
  { prefix: '', name: 'sample.txt' },
  { prefix: '', name: 'sample.json' },
  { prefix: '', name: 'sample.csv' },
  { prefix: '', name: 'sample.png' },
  { prefix: '', name: 'sample.pdf' },
  { prefix: '', name: 'sample.parquet' },
  { prefix: 'docs/', name: 'sample.txt' },
  { prefix: 'images/', name: 'sample.png' },
];

export const ADMIN = {
  username: 'admin',
  password: 'password',
} as const;

export const VIEWER = {
  username: 'e2e-viewer',
  password: 'viewerpass123',
} as const;

export function testDataPath(filename: string): string {
  return path.resolve(__dirname, '..', 'test-data', filename);
}
