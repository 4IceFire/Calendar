const {test, expect} = require('@playwright/test');
const {
  collectPageErrors,
  installCommonReadMocks,
  openProtectedPage,
} = require('./helpers');

const ROUTING_STATE = {
  ok: true,
  configured: true,
  stale: false,
  refreshing: false,
  inputs: [
    {number: 1, label: 'Stage'},
    {number: 2, label: 'Playback'},
  ],
  outputs: [
    {number: 1, label: 'Auditorium'},
    {number: 2, label: 'Foyer'},
  ],
  routing: [1, 2],
};

test.beforeEach(async ({page}) => {
  await installCommonReadMocks(page);
});

test('Routing initializes and discovers outputs in every browser', async ({page}) => {
  const pageErrors = collectPageErrors(page);
  await page.route('**/api/videohub/state**', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify(ROUTING_STATE),
  }));

  const access = await openProtectedPage(page, '/routing');
  test.skip(!access.ok, access.reason);
  await expect(page.locator('#routing-outputs .routing-choice')).toHaveCount(2);
  await expect(page.locator('#routing-outputs')).toContainText('1: Auditorium');
  await expect(page.locator('#routing-outputs')).toContainText('2: Foyer');
  expect(pageErrors).toEqual([]);
});

test('Routing presents a failure and recovers after a refresh', async ({page}) => {
  let attempt = 0;
  await page.route('**/api/videohub/state**', route => {
    attempt += 1;
    if (attempt === 1) {
      return route.fulfill({
        status: 503,
        contentType: 'application/json',
        body: JSON.stringify({ok: false, error: 'Simulated VideoHub outage'}),
      });
    }
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(ROUTING_STATE),
    });
  });

  const access = await openProtectedPage(page, '/routing');
  test.skip(!access.ok, access.reason);
  await expect(page.locator('#routing-status')).toContainText('Simulated VideoHub outage');
  await page.reload({waitUntil: 'domcontentloaded'});
  await expect(page.locator('#routing-outputs .routing-choice')).toHaveCount(2);
  await expect(page.locator('#routing-status')).toBeEmpty();
});
