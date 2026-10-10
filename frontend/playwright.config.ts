import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './e2e',
  workers: 1,
  timeout: 90000,
  expect: { timeout: 30000 },
  outputDir: process.env.TTAL_BROWSER_OUTPUT || '../artifacts/runtime/browser-results',
  reporter: 'list',
  use: {
    baseURL: process.env.TTAL_FRONTEND_URL || 'http://127.0.0.1:5173',
    browserName: 'chromium',
    channel: process.platform === 'win32' ? 'msedge' : undefined,
    headless: true,
    viewport: { width: 1440, height: 1000 },
    screenshot: 'only-on-failure',
  },
});
