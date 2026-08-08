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

test('quiz panel uses a spacious high-contrast light theme', async () => {
  const html = await readHtml();

  assert.match(html, /--panel:#fff; --line:#dce1e7; --ink:#101418/);
  assert.match(html, /#side\{width:380px;[\s\S]*?padding:26px 24px;[\s\S]*?gap:18px/);
  assert.match(html, /h1\{font-size:19px/);
  assert.match(html, /\.choice\{[\s\S]*?min-height:44px;[\s\S]*?font-size:14px/);
});

test('view options are anchored to the bottom of the viewer', async () => {
  const html = await readHtml();
  const stageStart = html.indexOf('<div id="stage">');
  const stageEnd = html.indexOf('</div>\n</div>', stageStart);
  const viewOptions = html.indexOf('<details id="view-options"');

  assert.ok(viewOptions > stageStart && viewOptions < stageEnd);
  assert.match(html, /#view-options\{position:absolute;[\s\S]*?bottom:16px/);
});

test('viewer controls are unbundled behind a compact caret', async () => {
  const html = await readHtml();

  assert.match(html, /#view-options\{[\s\S]*?background:transparent;[\s\S]*?border:0/);
  assert.match(html, /#view-options summary::after\{content:"⌃"/);
  assert.match(html, /#view-options\[open\] summary::after\{content:"⌄"/);
  assert.match(html, /#view-options \.seg\{display:contents/);
});
