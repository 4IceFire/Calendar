const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const {test, expect} = require('@playwright/test');

// Exercise the shipped heartbeat with real redirects from a loopback fixture.
// No running TDeck server, credentials, or production devices are needed.
const template = fs.readFileSync(path.join(__dirname, '../../templates/base.html'), 'utf8');
const heartbeatBlock = [...template.matchAll(/<script\s*>([\s\S]*?)<\/script>/g)]
  .map(match => match[1])
  .find(script => script.includes("'/auth/ping'"));
if (!heartbeatBlock) throw new Error('The shared authentication heartbeat script was not found.');
const heartbeatScript = heartbeatBlock
  .replace(/{{\s*auth_enabled\s*\|\s*tojson\s*}}/g, 'true')
  .replace(/{{\s*is_authenticated\s*\|\s*tojson\s*}}/g, 'true');

const currentPath = '/personal-mixes?aux=7&view=levels#channel-3';
const scenarioServers = new WeakMap();

test.afterEach(async ({page}) => {
  const server = scenarioServers.get(page);
  if (server) {
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
  }
});

async function installScenario(page, redirects, keepExpiredPage = false) {
  const state = {authRequests: [], completedFetches: [], loginNavigations: []};
  page.on('requestfinished', request => {
    if (request.resourceType() === 'fetch' && !request.redirectedTo()) {
      state.completedFetches.push(request.url());
    }
  });
  const server = http.createServer((request, response) => {
    const url = new URL(request.url, state.origin);
    if (url.pathname === '/auth/ping' || url.pathname === '/auth/touch') {
      state.authRequests.push(url.pathname);
      const supplied = redirects[url.pathname];
      const destination = typeof supplied === 'function' ? supplied(state.origin) : supplied;
      response.writeHead(destination ? 302 : 204, destination ? {Location: destination} : {});
      response.end();
      return;
    }
    if (request.headers['sec-fetch-mode'] === 'navigate' && url.pathname === '/login') {
      state.loginNavigations.push(url.toString());
      if (keepExpiredPage) {
        // A 204 navigation leaves the current document alive, allowing another
        // heartbeat response to arrive before a login navigation completes.
        response.writeHead(204);
        response.end();
        return;
      }
    }
    response.writeHead(200, {
      'Content-Type': 'text/html',
      'Access-Control-Allow-Origin': '*',
    });
    response.end('<!doctype html><title>Heartbeat fixture</title><main>Fixture page</main>');
  });
  scenarioServers.set(page, server);
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  state.origin = `http://127.0.0.1:${server.address().port}`;
  await page.goto(state.origin + '/previous-page');
  await page.goto(state.origin + currentPath);
  return state;
}

async function startHeartbeat(page) {
  await page.evaluate(script => {
    const element = document.createElement('script');
    element.textContent = script;
    document.body.appendChild(element);
  }, heartbeatScript);
}

for (const endpoint of ['/auth/ping', '/auth/touch']) {
  test(`${endpoint} returns to the visible page after idle sign-in`, async ({page}) => {
    const state = await installScenario(page, {
      [endpoint]: '/login?timeout=1&next=%2Fauth%2Fping',
    });
    await startHeartbeat(page);

    await expect(page).toHaveURL(url => url.pathname === '/login');
    const login = new URL(page.url());
    expect(login.origin).toBe(state.origin);
    expect(login.searchParams.get('next')).toBe(currentPath);
    expect(login.searchParams.get('timeout')).toBe('1');
    expect(state.loginNavigations).toHaveLength(1);

    await page.goBack();
    await expect(page).toHaveURL(state.origin + '/previous-page');
  });
}

test('concurrent and later heartbeat redirects navigate to login only once', async ({page}) => {
  const state = await installScenario(page, {
    '/auth/ping': '/login?timeout=1&next=%2Fauth%2Fping',
    '/auth/touch': '/login?timeout=1&next=%2Fauth%2Ftouch',
  }, true);
  await startHeartbeat(page);
  await expect.poll(() => state.completedFetches.length).toBeGreaterThanOrEqual(2);
  await expect.poll(() => state.loginNavigations.length).toBe(1);

  const completedBefore = state.completedFetches.length;
  await page.evaluate(() => {
    document.dispatchEvent(new Event('visibilitychange'));
    document.dispatchEvent(new Event('visibilitychange'));
  });
  await expect.poll(() => state.completedFetches.length).toBeGreaterThanOrEqual(completedBefore + 2);
  await page.evaluate(() => new Promise(resolve => setTimeout(resolve, 0)));
  expect(state.loginNavigations).toHaveLength(1);
  const login = new URL(state.loginNavigations[0]);
  expect(login.searchParams.get('next')).toBe(currentPath);
  expect(login.searchParams.get('timeout')).toBe('1');
  await expect(page).toHaveURL(state.origin + currentPath);
});

for (const [name, destination] of [
  ['an unrelated page', '/unrelated?next=/login'],
  ['a different origin', base => base.replace('127.0.0.1', 'localhost') + '/login?timeout=1'],
]) {
  test(`ignores heartbeat redirect to ${name}`, async ({page}) => {
    const state = await installScenario(page, {
      '/auth/ping': destination,
      '/auth/touch': destination,
    });
    await startHeartbeat(page);
    await expect.poll(() => state.completedFetches.length).toBeGreaterThanOrEqual(2);
    await page.evaluate(() => new Promise(resolve => setTimeout(resolve, 0)));
    await expect(page).toHaveURL(state.origin + currentPath);
    expect(state.loginNavigations).toEqual([]);
  });
}
