const {defineConfig, devices} = require('@playwright/test');

const baseURL = process.env.TDECK_BASE_URL || 'http://127.0.0.1:5000';
const parsedBaseURL = new URL(baseURL);
const localHosts = new Set(['127.0.0.1', 'localhost', '::1']);

if (!localHosts.has(parsedBaseURL.hostname) && process.env.TDECK_ALLOW_REMOTE_BROWSER_TESTS !== '1') {
  throw new Error(
    'Refusing to run browser tests against a non-loopback host. ' +
    'Set TDECK_ALLOW_REMOTE_BROWSER_TESTS=1 only for an approved staging/test server.'
  );
}

module.exports = defineConfig({
  testDir: './tests/browser',
  outputDir: './test-results/playwright',
  timeout: 30000,
  expect: {timeout: 7500},
  fullyParallel: true,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 2 : 0,
  reporter: process.env.CI ? [['line'], ['html', {outputFolder: 'test-results/playwright-report', open: 'never'}]] : 'line',
  use: {
    baseURL,
    ignoreHTTPSErrors: process.env.TDECK_IGNORE_HTTPS_ERRORS === '1',
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
  },
  projects: [
    {name: 'chromium', use: {...devices['Desktop Chrome']}},
    {name: 'firefox', use: {...devices['Desktop Firefox']}},
    {name: 'webkit', use: {...devices['Desktop Safari']}},
    {name: 'mobile-chromium', use: {...devices['Pixel 7']}},
    {name: 'mobile-webkit', use: {...devices['iPhone 13']}},
  ],
});
