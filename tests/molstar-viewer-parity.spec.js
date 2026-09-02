import { expect, test } from 'playwright/test';
import { mkdirSync, writeFileSync } from 'node:fs';
import path from 'node:path';

const BASELINE_URL = process.env.BASELINE_URL || 'https://www.foldarium.org/weekly';
const PREVIEW_URL = process.env.PREVIEW_URL || 'http://127.0.0.1:4350/weekly';
const OUTPUT_DIR = path.resolve('test-results/molstar-viewer-parity');
const RANDOM_SEED = 0x12345678;
const CONTROL_TOLERANCE_PX = 3;
const VIEWER_TIMEOUT_MS = 120_000;
const WEEKLY_GATE_PASSWORD = process.env.WEEKLY_GATE_PASSWORD;

function isIgnorableError(message) {
  return message.includes('molstarvolseg.ncbr.muni.cz')
    || message === 'Failed to load resource: net::ERR_FAILED'
    || message === 'Failed to fetch'
    || /^HTTP 404 .*\/api\/private-evaluation$/.test(message);
}

function filterErrors(errors) {
  return errors.filter(message => !isIgnorableError(message));
}

function closeEnough(left, right, tolerance = CONTROL_TOLERANCE_PX) {
  return Math.abs(left - right) <= tolerance;
}

async function createSession(browser, viewport) {
  const context = await browser.newContext({ viewport });
  await context.addInitScript(seed => {
    localStorage.clear();
    sessionStorage.clear();
    let state = seed >>> 0;
    Math.random = () => {
      state = (Math.imul(1664525, state) + 1013904223) >>> 0;
      return state / 4294967296;
    };
  }, RANDOM_SEED);
  const page = await context.newPage();
  await page.route('**/api/config', async route => {
    const response = await route.fetch();
    const config = await response.json();
    await route.fulfill({
      response,
      json: { ...config, writable: false },
    });
  });
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('console', message => {
    if (
      message.type() === 'error'
      && !message.text().startsWith('Failed to load resource:')
    ) errors.push(message.text());
  });
  page.on('response', response => {
    if (response.status() >= 400) {
      errors.push(`HTTP ${response.status()} ${response.url()}`);
    }
  });
  return { context, page, errors };
}

async function unlockWeekly(page, url) {
  await page.goto(url, { waitUntil: 'domcontentloaded', timeout: VIEWER_TIMEOUT_MS });
  await page.locator('#gate-pw').fill(WEEKLY_GATE_PASSWORD);
  await page.locator('#gate-form button[type="submit"]').click();
  await expect(page.locator('#participant-setup')).toBeVisible({ timeout: VIEWER_TIMEOUT_MS });
}

async function startQuiz(page, participantName) {
  await page.locator('#participant-name').fill(participantName);
  const performanceConsent = page.locator('#performance-consent-checkbox');
  if (await performanceConsent.isVisible()) await performanceConsent.check();
  await expect(page.locator('#start')).toBeEnabled({ timeout: VIEWER_TIMEOUT_MS });
  await page.locator('#start').click();
  await expect(page.locator('#progress')).toContainText('question 1 /', { timeout: VIEWER_TIMEOUT_MS });
}

async function waitForMolstarGrid(page) {
  await expect(page.locator('#gridview')).toHaveClass(/on/, { timeout: VIEWER_TIMEOUT_MS });
  await expect(page.locator('.grid-card').first()).toBeVisible({ timeout: VIEWER_TIMEOUT_MS });
  await page.waitForFunction(() => (
    !document.querySelector('#gridview')?.classList.contains('loading-grid')
    && !document.querySelector('#stage')?.classList.contains('loading-system')
    && !document.querySelector('#wrap')?.classList.contains('question-loading')
  ), null, { timeout: VIEWER_TIMEOUT_MS });
  await page.waitForFunction(() => {
    const cards = [...document.querySelectorAll('.grid-card')];
    return cards.length > 0
      && cards.every(card => card.classList.contains('failed') || card.querySelector('canvas'));
  }, null, { timeout: VIEWER_TIMEOUT_MS });
  await expect(page.locator('.grid-card.failed')).toHaveCount(0, { timeout: VIEWER_TIMEOUT_MS });
  await page.waitForTimeout(600);
}

async function waitForSingleViewer(page) {
  await expect(page.locator('#gridview')).not.toHaveClass(/on/, { timeout: VIEWER_TIMEOUT_MS });
  await page.waitForFunction(() => (
    !document.querySelector('#stage')?.classList.contains('loading-system')
    && !document.querySelector('#wrap')?.classList.contains('question-loading')
  ), null, { timeout: VIEWER_TIMEOUT_MS });
  await page.waitForFunction(() => !!document.querySelector('#app canvas'), null, {
    timeout: VIEWER_TIMEOUT_MS,
  });
  await page.waitForTimeout(600);
}

async function collectEvidence(page) {
  return page.evaluate(() => {
    const rect = element => {
      const value = element?.getBoundingClientRect();
      return value ? {
        x: Math.round(value.x),
        y: Math.round(value.y),
        width: Math.round(value.width),
        height: Math.round(value.height),
      } : null;
    };
    const visible = element => {
      if (!element) return false;
      const style = getComputedStyle(element);
      return style.display !== 'none' && style.visibility !== 'hidden';
    };
    const modeStates = Object.fromEntries(
      [...document.querySelectorAll('#mode button[data-m]')].map(button => [
        button.dataset.m,
        {
          pressed: button.getAttribute('aria-pressed') === 'true',
          on: button.classList.contains('on'),
        },
      ]),
    );
    const toggleIds = ['#uncluster', '#hbonds', '#surface', '#protein-ensemble'];
    const viewToggleStates = Object.fromEntries(
      toggleIds.map(selector => {
        const button = document.querySelector(selector);
        const id = selector.slice(1);
        if (!button || !visible(button)) return [id, null];
        return [id, {
          pressed: button.getAttribute('aria-pressed') === 'true',
          on: button.classList.contains('on'),
          label: button.textContent.trim(),
        }];
      }),
    );
    const controlSelectors = [
      '#mode button[data-m="grid"]',
      '#mode button[data-m="all"]',
      '#mode button[data-m="one"]',
      '#uncluster',
      '#hbonds',
      '#surface',
      '#protein-ensemble',
    ];
    const controls = controlSelectors
      .map(selector => document.querySelector(selector))
      .filter(element => visible(element))
      .map(rect);
    const controlLabels = controlSelectors
      .map(selector => document.querySelector(selector))
      .filter(element => visible(element))
      .map(element => element.textContent.replace(/\s+/g, ' ').trim());
    const choiceLabels = [...document.querySelectorAll('#choices .choice .nm')]
      .map(element => element.textContent.replace(/\s+/g, ' ').trim());
    const badge = document.querySelector('#badge');
    const badgeStyle = badge ? getComputedStyle(badge) : null;
    return {
      viewport: { width: innerWidth, height: innerHeight },
      progress: document.querySelector('#progress')?.textContent?.trim() || '',
      poseSummary: document.querySelector('#viewer-question .lig')?.textContent?.trim() || '',
      ligand: document.querySelector('#ligand')?.textContent?.trim() || '',
      modeStates,
      viewToggleStates,
      controls,
      controlLabels,
      choiceLabels,
      badgeText: badgeStyle?.display !== 'none' ? badge?.textContent?.trim() || '' : '',
      badgeVisible: badgeStyle?.display !== 'none',
      gridCardCount: document.querySelectorAll('.grid-card').length,
      failedCards: document.querySelectorAll('.grid-card.failed').length,
      canvasCount: document.querySelectorAll('#app canvas, .grid-card canvas').length,
      inspectingCards: document.querySelectorAll('.grid-card.inspecting').length,
      documentOverflow: document.documentElement.scrollWidth > document.documentElement.clientWidth + 1,
      headerOverflow: [...document.querySelectorAll('.grid-head')]
        .some(header => header.scrollWidth > header.clientWidth + 1),
      sidebarOverflow: [...document.querySelectorAll('#choices .choice')]
        .some(choice => choice.scrollWidth > choice.clientWidth + 1),
    };
  });
}

async function readRoundMeta(page) {
  return page.evaluate(() => ({
    setupHint: document.querySelector('#setuphint')?.textContent?.trim() || '',
    revealedTitle: document.querySelector('#revealed-weekly-title')?.textContent?.trim() || '',
    retrospectiveHref: document.querySelector('#current-retrospective-link')?.getAttribute('href') || '',
  }));
}

async function screenshot(page, label, name) {
  await page.screenshot({
    path: path.join(OUTPUT_DIR, `${label}-${name}.png`),
    fullPage: true,
    animations: 'disabled',
  });
}

async function clickCanvas(page, xRatio, yRatio) {
  const canvas = page.locator('#app canvas').first();
  const bounds = await canvas.boundingBox();
  if (!bounds) return false;
  await page.mouse.click(
    bounds.x + bounds.width * xRatio,
    bounds.y + bounds.height * yRatio,
  );
  await page.waitForTimeout(900);
  return true;
}

async function exerciseViewer(page, { label, reduced }) {
  const checkpoints = {};
  const record = async (name) => {
    checkpoints[name] = await collectEvidence(page);
    await screenshot(page, label, name);
  };

  await waitForMolstarGrid(page);
  await record('01-initial-grid');

  const uncluster = page.locator('#uncluster');
  if (await uncluster.isVisible()) {
    await uncluster.click();
    await waitForMolstarGrid(page);
    await record('02-uncluster-on');
    await uncluster.click();
    await waitForMolstarGrid(page);
    await record('03-uncluster-restored');
  }

  await page.locator('#mode button[data-m="all"]').click();
  await waitForSingleViewer(page);
  await record('04-show-all');

  if (await clickCanvas(page, 0.50, 0.52)) {
    await record('05-show-all-pose-click');
  }
  if (await clickCanvas(page, 0.95, 0.85)) {
    await record('06-show-all-empty-click');
  }

  if (!reduced) {
    await page.locator('#mode button[data-m="one"]').click();
    await waitForSingleViewer(page);
    const beforeNav = await collectEvidence(page);
    checkpoints['07-one-at-a-time-before-nav'] = beforeNav;
    await screenshot(page, label, '07-one-at-a-time-before-nav');

    await page.keyboard.press('ArrowRight');
    await waitForSingleViewer(page);
    const afterNav = await collectEvidence(page);
    checkpoints['08-one-at-a-time-after-nav'] = afterNav;
    await screenshot(page, label, '08-one-at-a-time-after-nav');
    checkpoints.cameraNav = {
      badgeBefore: beforeNav.badgeText,
      badgeAfter: afterNav.badgeText,
      badgeChanged: beforeNav.badgeText !== afterNav.badgeText,
      modeStillOne: afterNav.modeStates.one?.pressed === true,
    };

    const proteinEnsemble = page.locator('#protein-ensemble');
    if (await proteinEnsemble.isVisible()) {
      await proteinEnsemble.click();
      await waitForSingleViewer(page);
      await record('09-protein-ensemble-on');
      await proteinEnsemble.click();
      await waitForSingleViewer(page);
      await record('10-protein-ensemble-off');
    }

    const hbonds = page.locator('#hbonds');
    if (await hbonds.isVisible()) {
      await hbonds.click();
      await expect(hbonds).toHaveClass(/on/);
      await waitForSingleViewer(page);
      await record('11-hbonds-on');
      await hbonds.click();
      await waitForSingleViewer(page);
      await record('12-hbonds-off');
    }

    const surface = page.locator('#surface');
    if (await surface.isVisible()) {
      await surface.click();
      await expect(surface).toHaveClass(/on/);
      await waitForSingleViewer(page);
      await record('13-surface-on');
      await surface.click();
      await waitForSingleViewer(page);
      await record('14-surface-off');
    }
  } else {
    await page.locator('#mode button[data-m="one"]').click();
    await waitForSingleViewer(page);
    await record('07-mobile-one-at-a-time');
  }

  await page.locator('#mode button[data-m="grid"]').click();
  await waitForMolstarGrid(page);
  await record(reduced ? '08-mobile-grid-return' : '15-grid-return');

  return checkpoints;
}

function compareControls(baseline, preview, checkpoint) {
  expect(preview.failedCards, `${checkpoint}: preview failed grid cards`).toBe(0);
  expect(baseline.failedCards, `${checkpoint}: baseline failed grid cards`).toBe(0);
  expect(preview.documentOverflow, `${checkpoint}: preview horizontal overflow`).toBe(false);
  expect(baseline.documentOverflow, `${checkpoint}: baseline horizontal overflow`).toBe(false);
  expect(preview.headerOverflow, `${checkpoint}: Grid header overflow parity`).toBe(
    baseline.headerOverflow,
  );
  expect(preview.sidebarOverflow, `${checkpoint}: sidebar overflow parity`).toBe(
    baseline.sidebarOverflow,
  );

  expect(preview.modeStates, `${checkpoint}: mode states`).toEqual(baseline.modeStates);
  expect(preview.viewToggleStates, `${checkpoint}: view toggle states`).toEqual(
    baseline.viewToggleStates,
  );
  expect(preview.controlLabels, `${checkpoint}: visible control labels`).toEqual(
    baseline.controlLabels,
  );
  expect(preview.controls.length, `${checkpoint}: visible control count`).toBe(
    baseline.controls.length,
  );

  for (let index = 0; index < baseline.controls.length; index += 1) {
    const left = baseline.controls[index];
    const right = preview.controls[index];
    expect(closeEnough(left.width, right.width), `${checkpoint}: control ${index} width`).toBe(true);
    expect(closeEnough(left.height, right.height), `${checkpoint}: control ${index} height`).toBe(
      true,
    );
  }

  if (baseline.choiceLabels.length) {
    expect(preview.choiceLabels, `${checkpoint}: sidebar choice order`).toEqual(
      baseline.choiceLabels,
    );
  }

  expect(preview.gridCardCount, `${checkpoint}: grid card count`).toBe(baseline.gridCardCount);
  expect(preview.progress, `${checkpoint}: progress text`).toBe(baseline.progress);
  expect(preview.poseSummary, `${checkpoint}: pose summary`).toBe(baseline.poseSummary);
}

function compareSessions(baseline, preview, viewportLabel) {
  expect(preview.roundMeta, `${viewportLabel}: round metadata`).toEqual(baseline.roundMeta);
  expect(preview.questionMeta.progress, `${viewportLabel}: question progress`).toBe(
    baseline.questionMeta.progress,
  );
  expect(preview.questionMeta.poseSummary, `${viewportLabel}: initial pose summary`).toBe(
    baseline.questionMeta.poseSummary,
  );
  expect(preview.questionMeta.choiceLabels, `${viewportLabel}: initial choice labels`).toEqual(
    baseline.questionMeta.choiceLabels,
  );

  for (const checkpoint of Object.keys(baseline.checkpoints).filter(
    key => baseline.checkpoints[key]?.failedCards !== undefined,
  )) {
    compareControls(
      baseline.checkpoints[checkpoint],
      preview.checkpoints[checkpoint],
      `${viewportLabel}/${checkpoint}`,
    );
  }

  const navBaseline = baseline.checkpoints.cameraNav;
  const navPreview = preview.checkpoints.cameraNav;
  if (navBaseline && navPreview) {
    expect(navBaseline.modeStillOne, `${viewportLabel}: baseline stayed in one-at-a-time`).toBe(
      true,
    );
    expect(navPreview.modeStillOne, `${viewportLabel}: stayed in one-at-a-time`).toBe(true);
  }
}

async function runParitySession(browser, { url, label, viewport, reduced }) {
  const { context, page, errors } = await createSession(browser, viewport);
  try {
    await unlockWeekly(page, url);
    const roundMeta = await readRoundMeta(page);
    await startQuiz(page, `Molstar parity ${label}`);
    await waitForMolstarGrid(page);
    const questionMeta = await collectEvidence(page);
    const checkpoints = await exerciseViewer(page, { label, reduced });
    return {
      label,
      url,
      roundMeta,
      questionMeta,
      checkpoints,
      errors: filterErrors(errors),
    };
  } finally {
    await context.close();
  }
}

async function runViewportParity(browser, viewport, { reduced, report, viewportLabel }) {
  const baseline = await runParitySession(browser, {
    url: BASELINE_URL,
    label: `baseline-${viewportLabel}`,
    viewport,
    reduced,
  });
  const preview = await runParitySession(browser, {
    url: PREVIEW_URL,
    label: `preview-${viewportLabel}`,
    viewport,
    reduced,
  });
  compareSessions(baseline, preview, viewportLabel);
  expect(baseline.errors, `${viewportLabel}: baseline page errors`).toEqual([]);
  expect(preview.errors, `${viewportLabel}: preview page errors`).toEqual([]);
  report[viewportLabel] = { baseline, preview };
}

test('deployed Weekly Mol* viewer matches local preview', async ({ browser }) => {
  test.skip(!WEEKLY_GATE_PASSWORD, 'WEEKLY_GATE_PASSWORD is required for the protected parity audit');
  test.setTimeout(900_000);
  mkdirSync(OUTPUT_DIR, { recursive: true });
  const report = {
    baselineUrl: BASELINE_URL,
    previewUrl: PREVIEW_URL,
    randomSeed: RANDOM_SEED,
    tolerancePx: CONTROL_TOLERANCE_PX,
  };

  await runViewportParity(
    browser,
    { width: 1440, height: 900 },
    { reduced: false, report, viewportLabel: 'desktop-1440x900' },
  );

  await runViewportParity(
    browser,
    { width: 390, height: 844 },
    { reduced: true, report, viewportLabel: 'mobile-390x844' },
  );

  writeFileSync(path.join(OUTPUT_DIR, 'report.json'), JSON.stringify(report, null, 2));
});
