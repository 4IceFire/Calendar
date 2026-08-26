const {test, expect} = require('@playwright/test');
const {
  collectPageErrors,
  installCommonReadMocks,
  openProtectedPage,
} = require('./helpers');

const AUDIO_STATE = {
  ok: true,
  connected: true,
  stale: false,
  refreshing: false,
  sources: [
    {id: 'master', label: 'Master', kind: 'master', volume: -6, muted: false, level: {left: -20, right: -19}},
    {id: '1', label: 'Lectern', kind: 'input', volume: -12, muted: false, mixOption: 'on', level: {left: -30, right: -29}},
  ],
  monitor: {enabled: true, dim: false, volume: -18, solo: false, soloSource: ''},
  metering: {enabled: true, connected: true, active: true},
  sampledAt: Date.now() / 1000,
  ageMs: 0,
};

const METER_STATE = {
  ok: true,
  connected: true,
  stale: false,
  master: {left: -18, right: -17},
  sources: {'1': {left: -28, right: -27}},
  metering: {enabled: true, connected: true, active: true},
};

async function installAudioMocks(page, counters, failFirstState) {
  await page.route('**/api/atem/audio/state**', route => {
    counters.state += 1;
    if (failFirstState && counters.state === 1) {
      return route.fulfill({
        status: 503,
        contentType: 'application/json',
        body: JSON.stringify({ok: false, error: 'Simulated ATEM outage'}),
      });
    }
    return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(AUDIO_STATE)});
  });
  await page.route('**/api/atem/audio/meters**', route => {
    counters.meters += 1;
    return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(METER_STATE)});
  });
}

test.beforeEach(async ({page}) => {
  await installCommonReadMocks(page);
});

test('Record Audio initializes and recovers from a failed state request', async ({page}) => {
  const pageErrors = collectPageErrors(page);
  const counters = {state: 0, meters: 0};
  await installAudioMocks(page, counters, true);

  const access = await openProtectedPage(page, '/foyer-audio');
  test.skip(!access.ok, access.reason);
  await expect(page.locator('.foyer-audio-strip')).toHaveCount(2);
  await expect(page.locator('#foyer-audio-grid')).toContainText('Lectern');
  expect(counters.state).toBeGreaterThanOrEqual(2);
  expect(pageErrors).toEqual([]);
});

test('Record Audio pauses high-rate reads while the page is hidden', async ({page}) => {
  await page.addInitScript(() => {
    let simulatedHidden = false;
    Object.defineProperty(Document.prototype, 'hidden', {
      configurable: true,
      get: () => simulatedHidden,
    });
    Object.defineProperty(Document.prototype, 'visibilityState', {
      configurable: true,
      get: () => simulatedHidden ? 'hidden' : 'visible',
    });
    window.__setTDeckTestHidden = value => {
      simulatedHidden = Boolean(value);
      document.dispatchEvent(new Event('visibilitychange'));
    };
  });
  const counters = {state: 0, meters: 0};
  await installAudioMocks(page, counters, false);

  const access = await openProtectedPage(page, '/foyer-audio');
  test.skip(!access.ok, access.reason);
  await expect(page.locator('.foyer-audio-strip')).toHaveCount(2);
  await expect.poll(() => counters.meters).toBeGreaterThan(0);

  await page.evaluate(() => window.__setTDeckTestHidden(true));
  // A request whose timer fired immediately before visibility changed may
  // still reach the route while AbortController is cancelling it.  Let that
  // single in-flight read settle, then assert that the visible-page cadence
  // does not continue in the background.
  await page.waitForTimeout(650);
  const hiddenCount = counters.state + counters.meters;
  await page.waitForTimeout(1200);
  expect(counters.state + counters.meters).toBe(hiddenCount);

  await page.evaluate(() => window.__setTDeckTestHidden(false));
  await expect.poll(() => counters.state).toBeGreaterThan(1);
});
