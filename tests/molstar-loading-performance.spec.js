import { expect, test } from 'playwright/test';

const baselineUrl = process.env.BASELINE_URL || 'https://www.foldarium.org/weekly';
const previewUrl = process.env.PREVIEW_URL || process.env.PERFORMANCE_URL
  || 'http://127.0.0.1:4350/weekly?perf=1';
const repeats = Number(process.env.PERFORMANCE_REPEATS || 2);
const minimumImprovement = Number(process.env.PERFORMANCE_MIN_IMPROVEMENT || 0.30);
const setupDwellMs = Number(process.env.PERFORMANCE_SETUP_DWELL_MS || 10_000);
const questionDwellMs = Number(process.env.PERFORMANCE_QUESTION_DWELL_MS || 15_000);
const weeklyGatePassword = process.env.WEEKLY_GATE_PASSWORD;

function median(values) {
  const sorted = [...values].sort((left, right) => left - right);
  return sorted[Math.floor(sorted.length / 2)];
}

async function waitForGridVisible(page) {
  await page.waitForFunction(() => {
    const grid = document.querySelector('#gridview');
    const cells = document.querySelector('#gridcells');
    const cards = [...document.querySelectorAll('.grid-card')];
    if (!grid?.classList.contains('on') || !cards.length
        || Number(getComputedStyle(cells).opacity) === 0) return false;
    return cards.every(card => {
      const style = getComputedStyle(card);
      return style.visibility !== 'hidden'
        && Number(style.opacity) !== 0
        && !!card.querySelector('canvas');
    });
  }, null, { timeout: 90_000 });
}

async function waitForFirstGridCardVisible(page) {
  await page.waitForFunction(() => {
    const grid = document.querySelector('#gridview');
    const cells = document.querySelector('#gridcells');
    if (!grid?.classList.contains('on') || Number(getComputedStyle(cells).opacity) === 0) return false;
    return [...document.querySelectorAll('.grid-card')].some(card => {
      const style = getComputedStyle(card);
      return style.visibility !== 'hidden'
        && Number(style.opacity) !== 0
        && !!card.querySelector('canvas');
    });
  }, null, { timeout: 90_000 });
}

async function waitForQuestionReady(page) {
  await page.waitForFunction(() => (
    !document.querySelector('#stage')?.classList.contains('loading-system')
    && !document.querySelector('#gridview')?.classList.contains('loading-grid')
    && !document.querySelector('#wrap')?.classList.contains('question-loading')
  ), null, { timeout: 90_000 });
}

async function benchmarkRun(browser, { url, label }) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  await context.addInitScript(() => {
    localStorage.clear();
    sessionStorage.clear();
    let state = 0x12345678;
    Math.random = () => {
      state = (1664525 * state + 1013904223) >>> 0;
      return state / 4294967296;
    };
  });
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

  await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 90_000 });
  await page.locator('#gate-pw').fill(weeklyGatePassword);
  await page.locator('#gate-form button[type="submit"]').click();
  await expect(page.locator('#participant-setup')).toBeVisible({ timeout: 90_000 });
  await page.locator('#participant-name').fill(
    `Performance ${label}`,
  );
  await expect(page.locator('#start')).toBeEnabled({ timeout: 90_000 });
  await page.waitForTimeout(setupDwellMs);
  const initialStartedAt = Date.now();
  await page.evaluate(() => document.querySelector('#start')?.click());
  await waitForFirstGridCardVisible(page);
  const initialFirstCardVisibleMs = Date.now() - initialStartedAt;
  await waitForGridVisible(page);
  const initialGridVisibleMs = Date.now() - initialStartedAt;
  await waitForQuestionReady(page);
  const initialQuestionReadyMs = Date.now() - initialStartedAt;
  await expect(page.locator('#progress')).toContainText('question 1 /', { timeout: 90_000 });

  await page.waitForTimeout(questionDwellMs);
  const beforeNavigation = await page.evaluate(() => performance.getEntriesByType('resource')
    .filter(entry => entry.name.includes('/storage/v1/object/public/'))
    .map(entry => ({ name: entry.name, transferSize: entry.transferSize })));
  const beforeNames = new Set(beforeNavigation.map(entry => entry.name));
  const beforeCount = beforeNavigation.length;
  const beforeResponseCount = structureResponses.length;

  const nextStartedAt = Date.now();
  navigationStarted = true;
  await page.evaluate(() => document.querySelector('#question-next')?.click());
  await waitForFirstGridCardVisible(page);
  const nextFirstCardVisibleMs = Date.now() - nextStartedAt;
  await waitForGridVisible(page);
  const nextGridVisibleMs = Date.now() - nextStartedAt;
  await waitForQuestionReady(page);
  const nextQuestionReadyMs = Date.now() - nextStartedAt;
  await expect(page.locator('#progress')).toContainText('question 2 /', { timeout: 90_000 });

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
  const viewerReports = await page.evaluate(() => (
    (window.FOLDARIUM_PERFORMANCE_REPORTS || []).map(report => ({
      id: report.id,
      stage: report.stage,
      metadata: report.metadata,
      totalMs: report.totalMs,
      milestones: report.milestones,
      stageSummary: report.stageSummary,
    }))
  ));
  await context.close();
  return {
    initialFirstCardVisibleMs,
    initialGridVisibleMs,
    initialQuestionReadyMs,
    nextFirstCardVisibleMs,
    nextGridVisibleMs,
    nextQuestionReadyMs,
    duplicateTransferBytes,
    navigationTransferBytes,
    duplicateBodyTransferCount: duplicateBodyTransfers.length,
    conditionalCacheValidations,
    prefetchedResourceCount: beforeNavigation.length,
    failedStructures,
    viewerReports,
  };
}

test('preview improves the real Grid path without initial regression', async ({ browser }) => {
  test.skip(!weeklyGatePassword, 'WEEKLY_GATE_PASSWORD is required for the protected benchmark');
  test.setTimeout(900_000);
  const baseline = [];
  const preview = [];
  for (let index = 0; index < repeats; index += 1) {
    if (index % 2 === 0) {
      preview.push(await benchmarkRun(browser, { url: previewUrl, label: 'preview' }));
      baseline.push(await benchmarkRun(browser, { url: baselineUrl, label: 'baseline' }));
    } else {
      baseline.push(await benchmarkRun(browser, { url: baselineUrl, label: 'baseline' }));
      preview.push(await benchmarkRun(browser, { url: previewUrl, label: 'preview' }));
    }
  }

  const report = {
    baseline,
    preview,
    medianInitialFirstCardBaselineMs: median(baseline.map(run => run.initialFirstCardVisibleMs)),
    medianInitialFirstCardPreviewMs: median(preview.map(run => run.initialFirstCardVisibleMs)),
    medianInitialGridBaselineMs: median(baseline.map(run => run.initialGridVisibleMs)),
    medianInitialGridPreviewMs: median(preview.map(run => run.initialGridVisibleMs)),
    medianInitialQuestionBaselineMs: median(baseline.map(run => run.initialQuestionReadyMs)),
    medianInitialQuestionPreviewMs: median(preview.map(run => run.initialQuestionReadyMs)),
    medianNextFirstCardBaselineMs: median(baseline.map(run => run.nextFirstCardVisibleMs)),
    medianNextFirstCardPreviewMs: median(preview.map(run => run.nextFirstCardVisibleMs)),
    medianNextGridBaselineMs: median(baseline.map(run => run.nextGridVisibleMs)),
    medianNextGridPreviewMs: median(preview.map(run => run.nextGridVisibleMs)),
  };
  report.nextGridImprovement = 1
    - report.medianNextGridPreviewMs / report.medianNextGridBaselineMs;
  console.log(`MOLSTAR_PERFORMANCE ${JSON.stringify(report)}`);

  expect(preview.flatMap(run => run.failedStructures)).toEqual([]);
  expect(report.medianInitialFirstCardPreviewMs)
    .toBeLessThanOrEqual(report.medianInitialGridPreviewMs);
  expect(report.medianNextFirstCardPreviewMs)
    .toBeLessThanOrEqual(report.medianNextGridPreviewMs);
  expect(report.medianInitialGridPreviewMs)
    .toBeLessThanOrEqual(report.medianInitialGridBaselineMs * 1.10);
  expect(report.medianInitialQuestionPreviewMs)
    .toBeLessThanOrEqual(report.medianInitialQuestionBaselineMs * 1.10);
  expect(report.nextGridImprovement).toBeGreaterThanOrEqual(minimumImprovement);
});
