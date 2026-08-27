import { test, expect } from 'playwright/test';
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import path from 'node:path';
import { upgradeLocalPrivateReviewBundle } from '../local-private-review-bundle.js';

const baselineUrl = process.env.BASELINE_URL || 'http://127.0.0.1:4318/weekly';
const retrospectiveUrl = process.env.RETROSPECTIVE_URL || 'http://127.0.0.1:4317/weekly';
const bundlePath = process.env.PRIVATE_BUNDLE_PATH;
const outputDir = process.env.VISUAL_AUDIT_OUTPUT
  || path.resolve('test-results/visual-parity-audit');
const supabaseUrl = process.env.VISUAL_AUDIT_SUPABASE_URL || '';
const publishableKey = process.env.VISUAL_AUDIT_SUPABASE_PUBLISHABLE_KEY || '';
const browserConfig = {
  url: supabaseUrl,
  publishableKey,
  structureBaseUrl: process.env.VISUAL_AUDIT_STRUCTURE_BASE_URL
    || `${supabaseUrl.replace(/\/$/, '')}/storage/v1/object/public/structures`,
  enabled: true,
  writable: false,
  deploymentEnvironment: 'production',
  commitSha: '',
};

test.skip(
  !supabaseUrl || !publishableKey,
  'Set VISUAL_AUDIT_SUPABASE_URL and VISUAL_AUDIT_SUPABASE_PUBLISHABLE_KEY',
);

function privateBundleBody() {
  if (!bundlePath) throw new Error('PRIVATE_BUNDLE_PATH is required');
  const output = readFileSync(bundlePath, 'utf8');
  const marker = '{"format_version":"foldarium.weekly-private-evaluation/';
  const start = output.indexOf(marker);
  if (start < 0) throw new Error('Private evaluation bundle was not found in command output');
  const bundle = upgradeLocalPrivateReviewBundle(JSON.parse(output.slice(start).trim()));
  return JSON.stringify(bundle);
}

async function configurePage(page, { retrospective = false } = {}) {
  await page.route('**/api/config', route => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({
      ...browserConfig,
      deploymentEnvironment: retrospective ? 'preview' : 'production',
    }),
  }));
  if (retrospective) {
    const body = privateBundleBody();
    await page.route('**/api/private-evaluation', route => route.fulfill({
      status: 200,
      contentType: 'application/json',
      body,
    }));
  }
}

async function unlock(page, url) {
  await page.goto(url, { waitUntil: 'domcontentloaded' });
  await expect(page.locator('#wrap')).toBeVisible({ timeout: 30_000 });
}

async function waitForGrid(page) {
  await expect(page.locator('#gridview')).toHaveClass(/on/, { timeout: 45_000 });
  await expect(page.locator('.grid-card').first()).toBeVisible({ timeout: 45_000 });
  await page.waitForFunction(() => (
    !document.querySelector('#gridview')?.classList.contains('loading-grid')
    && !document.querySelector('#stage')?.classList.contains('loading-system')
  ), null, { timeout: 45_000 });
  await expect(page.locator('#mode button[data-m="grid"]')).toBeEnabled({ timeout: 45_000 });
  await page.waitForTimeout(800);
}

async function waitForSingle(page) {
  await expect(page.locator('#gridview')).not.toHaveClass(/on/, { timeout: 45_000 });
  await page.waitForFunction(() => (
    !document.querySelector('#stage')?.classList.contains('loading-system')
  ), null, { timeout: 45_000 });
  await page.waitForTimeout(800);
}

async function screenshot(page, name) {
  await page.screenshot({
    path: path.join(outputDir, `${name}.png`),
    fullPage: true,
    animations: 'disabled',
  });
}

async function layoutMetrics(page) {
  return page.evaluate(() => {
    const rect = element => {
      const value = element?.getBoundingClientRect();
      return value ? {
        x: value.x, y: value.y, width: value.width, height: value.height,
        right: value.right, bottom: value.bottom,
      } : null;
    };
    const overlaps = (left, right) => !!(left && right
      && left.x < right.right && left.right > right.x
      && left.y < right.bottom && left.bottom > right.y);
    const cards = [...document.querySelectorAll('.grid-card')];
    const actionButtons = [...document.querySelectorAll('.grid-review-actions button')];
    const standardButtons = [...document.querySelectorAll('#mode button, #controls button')];
    const viewerQuestion = rect(document.querySelector('#viewer-question'));
    const badge = rect(document.querySelector('#badge:not([style*="display: none"])'));
    const instructionElement = document.querySelector('#instruction');
    const instructionStyle = instructionElement ? getComputedStyle(instructionElement) : null;
    const instructionRect = rect(instructionElement);
    return {
      viewport: { width: innerWidth, height: innerHeight },
      documentOverflow: document.documentElement.scrollWidth > document.documentElement.clientWidth,
      cardRects: cards.map(rect),
      actionRects: actionButtons.map(rect),
      actionLabels: actionButtons.map(button => button.textContent.trim()),
      standardControlRects: standardButtons.map(rect),
      headerOverflow: [...document.querySelectorAll('.grid-head')]
        .map(header => header.scrollWidth > header.clientWidth),
      sidebarOverflow: [...document.querySelectorAll('#answer-choices .choice')]
        .map(choice => choice.scrollWidth > choice.clientWidth),
      instruction: instructionRect && instructionStyle?.display !== 'none' ? {
        text: instructionElement.textContent.trim(),
        rect: instructionRect,
        lineHeight: Number.parseFloat(instructionStyle.lineHeight),
        estimatedLines: Math.round(instructionRect.height / Number.parseFloat(instructionStyle.lineHeight)),
      } : null,
      emptyVerdictVisible: (() => {
        const verdict = document.querySelector('#verdict');
        return !!(verdict && getComputedStyle(verdict).display !== 'none'
          && !verdict.textContent.trim() && rect(verdict)?.height > 0);
      })(),
      topbarOverlap: overlaps(viewerQuestion, badge),
      failedCards: document.querySelectorAll('.grid-card.failed').length,
    };
  });
}

test('deployed Weekly and retrospective preserve viewer layout semantics', async ({ browser }) => {
  test.setTimeout(240_000);
  mkdirSync(outputDir, { recursive: true });
  const report = { baseline: {}, retrospective: {}, errors: [] };

  const baselineContext = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const baseline = await baselineContext.newPage();
  baseline.on('pageerror', error => report.errors.push(`baseline: ${error.message}`));
  await configurePage(baseline);
  await unlock(baseline, baselineUrl);
  await expect(baseline.locator('#participant-setup')).toBeVisible({ timeout: 30_000 });
  await baseline.locator('#participant-name').fill('Visual audit');
  await expect(baseline.locator('#start')).toBeEnabled({ timeout: 30_000 });
  await baseline.locator('#start').click();
  await baseline.locator('#mode button[data-m="grid"]').click();
  await waitForGrid(baseline);
  report.baseline.grid = await layoutMetrics(baseline);
  await screenshot(baseline, 'baseline-grid');
  await baseline.locator('#mode button[data-m="all"]').click();
  await waitForSingle(baseline);
  await screenshot(baseline, 'baseline-show-all-initial');
  await baseline.locator('#mode button[data-m="one"]').click();
  await waitForSingle(baseline);
  await screenshot(baseline, 'baseline-one-at-a-time');
  await baselineContext.close();

  const reviewContext = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const review = await reviewContext.newPage();
  review.on('pageerror', error => report.errors.push(`retrospective: ${error.message}`));
  await configurePage(review, { retrospective: true });
  await unlock(review, retrospectiveUrl);
  await expect(review.locator('#private-review-banner')).toHaveAttribute('data-active', 'true', {
    timeout: 45_000,
  });
  await expect(review.locator('#participant-setup')).toBeHidden();
  await expect(review.locator('#start')).toBeEnabled({ timeout: 30_000 });
  await review.locator('#start').click();
  await review.locator('#mode button[data-m="grid"]').click();
  await waitForGrid(review);
  report.retrospective.grid = await layoutMetrics(review);
  await screenshot(review, 'retrospective-grid');

  const baselineButtons = report.baseline.grid.standardControlRects;
  const reviewButtons = report.retrospective.grid.actionRects;
  expect(report.retrospective.grid.documentOverflow).toBe(false);
  expect(report.retrospective.grid.headerOverflow.every(value => !value)).toBe(true);
  expect(report.retrospective.grid.sidebarOverflow.every(value => !value)).toBe(true);
  expect(report.retrospective.grid.topbarOverlap).toBe(false);
  expect(report.retrospective.grid.failedCards).toBe(0);
  expect(new Set(report.retrospective.grid.actionLabels)).toEqual(new Set(['Xtal', 'Folded']));
  expect(Math.max(...reviewButtons.map(button => button.height)))
    .toBeLessThanOrEqual(Math.max(...baselineButtons.map(button => button.height)) + 2);

  const firstFoldedButton = review.locator('.grid-card .grid-review-actions button', {
    hasText: 'Folded',
  }).first();
  await firstFoldedButton.click();
  await review.waitForTimeout(1800);
  report.retrospective.gridFoldedActivated = await firstFoldedButton.getAttribute('aria-pressed') === 'true';
  await screenshot(review, 'retrospective-grid-first-card-folded');
  if (report.retrospective.gridFoldedActivated) {
    await review.locator('.grid-card .grid-review-actions button', { hasText: 'Xtal' }).first().click();
    await review.waitForTimeout(1200);
  }

  await review.locator('#surface').click();
  await expect(review.locator('#surface')).toHaveClass(/on/);
  await review.waitForTimeout(1200);
  await screenshot(review, 'retrospective-grid-surface');
  await review.locator('#surface').click();

  await review.locator('#hbonds').click();
  await expect(review.locator('#hbonds')).toHaveClass(/on/);
  await review.waitForTimeout(1800);
  await screenshot(review, 'retrospective-grid-hbonds');
  await review.locator('#hbonds').click();

  await review.locator('#uncluster').click();
  await expect(review.locator('#uncluster')).toHaveClass(/on/);
  await waitForGrid(review);
  await screenshot(review, 'retrospective-grid-unclustered');
  await review.locator('#uncluster').click();
  await waitForGrid(review);

  await review.locator('#mode button[data-m="all"]').click();
  await waitForSingle(review);
  await screenshot(review, 'retrospective-show-all-initial');
  const canvas = review.locator('#app canvas').first();
  const canvasBox = await canvas.boundingBox();
  if (canvasBox) {
    await review.mouse.click(
      canvasBox.x + canvasBox.width * 0.50,
      canvasBox.y + canvasBox.height * 0.52,
    );
    await review.waitForTimeout(1200);
    await screenshot(review, 'retrospective-show-all-after-center-click');
  }

  await review.locator('#mode button[data-m="one"]').click();
  await waitForSingle(review);
  await screenshot(review, 'retrospective-one-at-a-time-xtal');
  await review.locator('#one-reject').click();
  await waitForSingle(review);
  await screenshot(review, 'retrospective-one-at-a-time-folded');

  await review.locator('#mode button[data-m="grid"]').click();
  await waitForGrid(review);
  await review.setViewportSize({ width: 1024, height: 768 });
  await review.waitForTimeout(500);
  report.retrospective.narrowGrid = await layoutMetrics(review);
  await screenshot(review, 'retrospective-grid-narrow');
  expect(report.retrospective.narrowGrid.documentOverflow).toBe(false);
  expect(report.retrospective.narrowGrid.topbarOverlap).toBe(false);

  writeFileSync(path.join(outputDir, 'report.json'), JSON.stringify(report, null, 2));
  const unexpectedErrors = report.errors.filter(message => !message.endsWith('Failed to fetch'));
  await reviewContext.close();
  expect(unexpectedErrors).toEqual([]);
});

test('retrospective keeps a nine-card page in a three-by-three Grid', async ({ browser }) => {
  test.setTimeout(180_000);
  mkdirSync(outputDir, { recursive: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await context.newPage();
  const pageErrors = [];
  const consoleMessages = [];
  page.on('pageerror', error => pageErrors.push(error.message));
  page.on('console', message => {
    if (message.type() === 'warning' || message.type() === 'error') {
      consoleMessages.push(message.text());
    }
  });
  await configurePage(page, { retrospective: true });
  await unlock(page, retrospectiveUrl);
  await expect(page.locator('#private-review-banner')).toHaveAttribute('data-active', 'true', {
    timeout: 45_000,
  });
  await expect(page.locator('#start')).toBeEnabled({ timeout: 30_000 });
  await page.locator('#start').click();
  await page.locator('#mode button[data-m="grid"]').click();
  await waitForGrid(page);
  await expect(page.locator('#instruction')).toBeHidden();
  await expect(page.locator('#verdict')).toBeHidden();
  await expect(page.locator('#ligand a')).toHaveText(/^[A-Z0-9]{4} ↗$/);
  await expect(page.locator('#ligand a')).toHaveAttribute(
    'href',
    /^https:\/\/www\.rcsb\.org\/structure\/[A-Z0-9]{4}$/,
  );
  await expect(page.locator('#weekly-leaderboard')).toContainText('Smina');
  await expect(page.locator('#weekly-leaderboard')).not.toContainText('scope unknown');
  const correctBorder = await page.locator('#answer-choices .choice.correct').first().evaluate(node => {
    const style = getComputedStyle(node);
    return {
      topColor: style.borderTopColor,
      leftColor: style.borderLeftColor,
      topWidth: style.borderTopWidth,
      leftWidth: style.borderLeftWidth,
    };
  });
  expect(correctBorder.topColor).toBe(correctBorder.leftColor);
  expect(correctBorder.topWidth).toBe('2px');
  expect(correctBorder.leftWidth).toBe('5px');
  const wrongBorder = await page.locator('#answer-choices .choice.wrong').first().evaluate(node => {
    const style = getComputedStyle(node);
    return { topWidth: style.borderTopWidth, leftWidth: style.borderLeftWidth };
  });
  expect(wrongBorder.topWidth).toBe('1px');
  expect(wrongBorder.leftWidth).toBe('5px');
  await screenshot(page, 'retrospective-grid-rmsd-xtal');
  const clusteredSidebarCount = await page.locator('#answer-choices .choice').count();

  const folded = page.locator('.grid-card .grid-review-actions button', {
    hasText: 'Folded',
  }).first();
  await folded.click();
  await page.waitForTimeout(5000);
  const foldedActive = await folded.getAttribute('aria-pressed') === 'true';
  expect(foldedActive, consoleMessages.join('\n')).toBe(true);
  await page.locator('.grid-card .grid-review-actions button', { hasText: 'Xtal' }).first().click();
  await page.locator('#uncluster').click();
  await expect(page.locator('#uncluster')).toHaveClass(/on/);
  await waitForGrid(page);
  const unclusteredSidebarCount = await page.locator('#answer-choices .choice').count();
  expect(unclusteredSidebarCount).toBeGreaterThan(clusteredSidebarCount);

  const positions = await page.locator('.grid-card').evaluateAll(cards => cards.map(card => {
    const bounds = card.getBoundingClientRect();
    return { x: Math.round(bounds.x), y: Math.round(bounds.y) };
  }));
  const distinct = values => new Set(values).size;
  expect(positions).toHaveLength(9);
  expect(distinct(positions.map(position => position.x))).toBe(3);
  expect(distinct(positions.map(position => position.y))).toBe(3);
  expect(pageErrors.filter(message => message.includes('captureState'))).toEqual([]);
  await screenshot(page, 'retrospective-grid-3x3');
  await page.locator('#uncluster').click();
  await expect(page.locator('#uncluster')).not.toHaveClass(/on/);
  await waitForGrid(page);
  await expect(page.locator('#answer-choices .choice')).toHaveCount(clusteredSidebarCount);
  await context.close();
});

test('retrospective Show all resets focus on the first empty-space click', async ({ browser }) => {
  test.setTimeout(180_000);
  mkdirSync(outputDir, { recursive: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await context.newPage();
  await configurePage(page, { retrospective: true });
  await unlock(page, retrospectiveUrl);
  await expect(page.locator('#private-review-banner')).toHaveAttribute('data-active', 'true', {
    timeout: 45_000,
  });
  await expect(page.locator('#start')).toBeEnabled({ timeout: 30_000 });
  await page.locator('#start').click();
  await page.locator('#mode button[data-m="all"]').click();
  await waitForSingle(page);
  const canvas = page.locator('#app canvas').first();
  const bounds = await canvas.boundingBox();
  expect(bounds).not.toBeNull();

  await page.mouse.click(bounds.x + bounds.width * 0.50, bounds.y + bounds.height * 0.52);
  await expect(page.locator('#mode button[data-m="all"]')).toBeEnabled({ timeout: 45_000 });
  await page.waitForTimeout(800);
  await screenshot(page, 'retrospective-show-all-focused');

  await page.mouse.click(bounds.x + bounds.width * 0.95, bounds.y + bounds.height * 0.85);
  await expect(page.locator('#mode button[data-m="all"]')).toBeEnabled({ timeout: 45_000 });
  await page.waitForTimeout(1200);
  await screenshot(page, 'retrospective-show-all-reset-once');
  await context.close();
});
