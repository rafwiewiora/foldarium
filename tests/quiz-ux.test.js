import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const readHtml = () => readFile(new URL('../index.html', import.meta.url), 'utf8');
const readApp = () => readFile(new URL('../app.js', import.meta.url), 'utf8');

test('quiz exposes a minimal primary flow and hides technical controls', async () => {
  const html = await readHtml();

  assert.match(html, />Dataset</);
  assert.match(html, />Difficulty</);
  assert.match(html, /Pick the pose that best fits the binding pocket\./);
  assert.match(html, /<details id="view-options"/);
  assert.match(html, /<details id="answer-details"/);
  assert.match(html, />Submit answer</);
  assert.match(html, /loadScript\('app\.js\?v=\d+'\)/);
});

test('quiz has responsive and accessible state cues', async () => {
  const [html, app] = await Promise.all([readHtml(), readApp()]);

  assert.match(html, /@media \(max-width: 760px\)/);
  assert.match(html, /aria-live="polite"/);
  assert.match(app, /aria-pressed/);
  assert.match(app, /Selected/);
  assert.match(app, /Not quite/);
});
