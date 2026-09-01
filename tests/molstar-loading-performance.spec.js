import { expect, test } from 'playwright/test';

const previewUrl = process.env.PERFORMANCE_URL || 'http://127.0.0.1:4350/weekly';
const repeats = Number(process.env.PERFORMANCE_REPEATS || 3);
const minimumImprovement = Number(process.env.PERFORMANCE_MIN_IMPROVEMENT || 0.30);
const weeklyGatePassword = process.env.WEEKLY_GATE_PASSWORD;

function median(values) {
  const sorted = [...values].sort((left, right) => left - right);
  return sorted[Math.floor(sorted.length / 2)];
}

async function waitForViewer(page) {
  await page.waitForFunction(() => (
    !document.querySelector('#stage')?.classList.contains('loading-system')
    && !document.querySelector('#gridview')?.classList.contains('loading-grid')
    && !document.querySelector('#wrap')?.classList.contains('question-loading')
  ), null, { timeout: 90_000 });
}

async function benchmarkRun(browser, { disablePrefetch }) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  if (disablePrefetch) {
    await context.addInitScript(() => {
      Object.defineProperty(Navigator.prototype, 'connection', {
        configurable: true,
        get: () => ({ saveData: true, effectiveType: '4g' }),
      });
    });
  }
  const page = await context.newPage();
  await page.route('**/api/config', async route => {
    const response = await route.fetch();
    const config = await response.json();
    await route.fulfill({
      response,
      json: { ...config, writable: false },
    });
  });
  const cdp = await context.newCDPSession(page);
  const structureResponses = [];
  const structureResponseByRequest = new Map();
  const requestStartedBeforeNavigation = new Map();
  let navigationStarted = false;
  await cdp.send('Network.enable');
  cdp.on('Network.requestWillBeSent', event => {
    if (event.request.url.includes('/storage/v1/object/public/')) {
      requestStartedBeforeNavigation.set(event.requestId, !navigationStarted);
    }
  });
  cdp.on('Network.responseReceived', event => {
    if (event.response.url.includes('/storage/v1/object/public/')) {
      const record = {
        url: event.response.url,
        fromDiskCache: event.response.fromDiskCache === true,
        fromPrefetchCache: event.response.fromPrefetchCache === true,
        encodedDataLength: null,
        startedBeforeNavigation: requestStartedBeforeNavigation.get(event.requestId) !== false,
      };
      structureResponses.push(record);
      structureResponseByRequest.set(event.requestId, record);
    }
  });
  cdp.on('Network.loadingFinished', event => {
    const record = structureResponseByRequest.get(event.requestId);
    if (record) record.encodedDataLength = event.encodedDataLength;
  });
  if (process.env.PERFORMANCE_THROTTLE !== '0') {
    await cdp.send('Network.emulateNetworkConditions', {
      offline: false,
      latency: 250,
      downloadThroughput: 625_000,
      uploadThroughput: 625_000,
      connectionType: 'cellular3g',
    });
  }
  const failedStructures = [];
  page.on('response', response => {
    if (
      response.url().includes('/storage/v1/object/public/')
      && response.status() >= 400
    ) failedStructures.push({ url: response.url(), status: response.status() });
  });

  await page.goto(previewUrl, { waitUntil: 'domcontentloaded', timeout: 90_000 });
  await page.locator('#gate-pw').fill(weeklyGatePassword);
  await page.locator('#gate-form button[type="submit"]').click();
  await expect(page.locator('#participant-setup')).toBeVisible({ timeout: 90_000 });
  await page.locator('#participant-name').fill(
    disablePrefetch ? 'Performance baseline' : 'Performance preview',
  );
  await expect(page.locator('#start')).toBeEnabled({ timeout: 90_000 });
  const initialStartedAt = Date.now();
  await page.locator('#start').click();
  await expect(page.locator('#progress')).toContainText('question 1 /', { timeout: 90_000 });
  await waitForViewer(page);
  const initialReadyMs = Date.now() - initialStartedAt;

  await page.locator('#mode button[data-m="one"]').click();
  await waitForViewer(page);
  await page.waitForTimeout(disablePrefetch ? 500 : 12_000);
  const beforeNavigation = await page.evaluate(() => performance.getEntriesByType('resource')
    .filter(entry => entry.name.includes('/storage/v1/object/public/'))
    .map(entry => ({ name: entry.name, transferSize: entry.transferSize })));
  const beforeNames = new Set(beforeNavigation.map(entry => entry.name));
  const beforeCount = beforeNavigation.length;
  const beforeResponseCount = structureResponses.length;

  const nextStartedAt = Date.now();
  navigationStarted = true;
  await page.locator('#question-next').click();
  await expect(page.locator('#progress')).toContainText('question 2 /', { timeout: 90_000 });
  await waitForViewer(page);
  const nextReadyMs = Date.now() - nextStartedAt;

  const afterNavigation = await page.evaluate(start => (
    performance.getEntriesByType('resource')
      .filter(entry => entry.name.includes('/storage/v1/object/public/'))
      .slice(start)
      .map(entry => ({ name: entry.name, transferSize: entry.transferSize }))
  ), beforeCount);
  const duplicateTransferBytes = afterNavigation
    .filter(entry => beforeNames.has(entry.name))
    .reduce((total, entry) => total + entry.transferSize, 0);
  const navigationTransferBytes = afterNavigation
    .reduce((total, entry) => total + entry.transferSize, 0);
  const navigationResponses = structureResponses.slice(beforeResponseCount);
  const duplicateBodyTransfers = structureResponses.filter(response => (
    !response.startedBeforeNavigation
    && beforeNames.has(response.url)
    && response.encodedDataLength > 1_024
  ));
  const conditionalCacheValidations = navigationResponses.filter(response => (
    beforeNames.has(response.url)
    && response.encodedDataLength > 0
    && response.encodedDataLength <= 1_024
  )).length;
  await context.close();
  return {
    initialReadyMs,
    nextReadyMs,
    duplicateTransferBytes,
    navigationTransferBytes,
    duplicateBodyTransfers,
    conditionalCacheValidations,
    prefetchedResourceCount: beforeNavigation.length,
    failedStructures,
  };
}

test('prefetch improves next-question readiness without first-load regression', async ({ browser }) => {
  test.skip(!weeklyGatePassword, 'WEEKLY_GATE_PASSWORD is required for the protected benchmark');
  test.setTimeout(900_000);
  const baseline = [];
  const preview = [];
  for (let index = 0; index < repeats; index += 1) {
    baseline.push(await benchmarkRun(browser, { disablePrefetch: true }));
    preview.push(await benchmarkRun(browser, { disablePrefetch: false }));
  }

  const report = {
    baseline,
    preview,
    medianInitialBaselineMs: median(baseline.map(run => run.initialReadyMs)),
    medianInitialPreviewMs: median(preview.map(run => run.initialReadyMs)),
    medianNextBaselineMs: median(baseline.map(run => run.nextReadyMs)),
    medianNextPreviewMs: median(preview.map(run => run.nextReadyMs)),
  };
  report.nextReadinessImprovement = 1
    - report.medianNextPreviewMs / report.medianNextBaselineMs;
  console.log(`MOLSTAR_PERFORMANCE ${JSON.stringify(report)}`);

  expect(preview.flatMap(run => run.failedStructures)).toEqual([]);
  expect(preview.flatMap(run => run.duplicateBodyTransfers)).toEqual([]);
  expect(preview.reduce((total, run) => total + run.duplicateTransferBytes, 0)).toBe(0);
  expect(report.medianInitialPreviewMs)
    .toBeLessThanOrEqual(report.medianInitialBaselineMs * 1.10);
  expect(report.nextReadinessImprovement).toBeGreaterThanOrEqual(minimumImprovement);
});
