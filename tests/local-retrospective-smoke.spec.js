import { test, expect } from 'playwright/test';

test('stable local URL activates retrospective review', async ({ page }) => {
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('console', message => {
    if (message.type() === 'error') errors.push(message.text());
  });
  await page.goto(process.env.RETROSPECTIVE_URL || 'http://127.0.0.1:4319/weekly');
  await page.waitForTimeout(5_000);
  const status = await page.locator('#private-review-status').textContent();
  const relevantErrors = errors.filter(message => (
    !message.includes('molstarvolseg.ncbr.muni.cz')
    && message !== 'Failed to load resource: net::ERR_FAILED'
    && message !== 'Failed to fetch'
  ));
  expect(relevantErrors, status || 'Private review did not activate').toEqual([]);
  await expect(page.locator('#private-review-banner')).toBeVisible({ timeout: 10_000 });
  await expect(page.locator('#private-review-banner')).toContainText('Retrospective');
  const state = await page.evaluate(() => ({
    mode: window.FOLDARIUM_QUIZ_MODE,
    active: window.FOLDARIUM_PRIVATE_REVIEW?.active,
    wrapClass: document.querySelector('#wrap')?.className,
    startDisplay: getComputedStyle(document.querySelector('#start')).display,
  }));
  expect(state.mode, JSON.stringify(state)).toBe('weekly');
  expect(state.active, JSON.stringify(state)).toBe(true);
  expect(state.startDisplay, JSON.stringify(state)).toBe('none');
  expect(state.wrapClass, JSON.stringify(state)).not.toContain('intro');
});
