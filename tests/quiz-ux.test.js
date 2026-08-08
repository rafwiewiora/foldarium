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
  assert.match(html, /<div id="view-options"/);
  assert.match(html, /<details id="answer-details"/);
  assert.match(html, />Submit answer</);
  assert.match(html, /<button[^>]+id="surface"[^>]*>Surface<\/button>/);
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
  const viewOptions = html.indexOf('<div id="view-options"');

  assert.ok(viewOptions > stageStart && viewOptions < stageEnd);
  assert.match(html, /#view-options\{position:absolute;[\s\S]*?bottom:16px/);
});

test('viewer controls form an always-visible bottom toolbar', async () => {
  const html = await readHtml();

  assert.match(html, /#view-options\{[\s\S]*?display:flex;[\s\S]*?background:transparent;[\s\S]*?border:0/);
  assert.doesNotMatch(html, /<summary>View options<\/summary>/);
  assert.match(html, /#view-options \.seg\{display:contents/);
});

test('question context is arranged at the top of the viewer', async () => {
  const html = await readHtml();
  const stage = html.indexOf('<div id="stage">');
  const context = html.indexOf('<div id="viewer-question">');
  const ligand = html.indexOf('id="ligand"', context);
  const instruction = html.indexOf('id="instruction"', context);

  assert.ok(stage < context && context < ligand && ligand < instruction);
  assert.match(html, /#viewer-question\{position:absolute;[\s\S]*?top:16px/);
  assert.match(html, /\.badge\{[\s\S]*?right:16px/);
});

test('quiz chrome uses Geist Sans without changing the molecular viewer', async () => {
  const html = await readHtml();

  assert.match(html, /@font-face\{font-family:"Geist Sans";[\s\S]*?geist:vf@5\.3\.0/);
  assert.match(html, /#side,#viewer-question,#view-options,\.badge\{font-family:"Geist Sans"/);
  assert.doesNotMatch(html, /html,body\{[^}]*font-family:"Geist Sans"/);
});

test('left panel has a balanced type and gray hierarchy', async () => {
  const html = await readHtml();

  assert.match(html, /#side\{--ink:#171a1f;--muted:#66717f;--faint:#8a94a3;--line:#e2e6eb/);
  assert.match(html, /#side h1\{font-size:18px/);
  assert.match(html, /#side \.sub\{font-size:12\.5px/);
  assert.match(html, /#side \.q\{font-size:11\.5px/);
  assert.match(html, /#side \.choice\{font-size:14px;line-height:1\.4/);
});

test('left pose selections use stacked menu rows with arrows', async () => {
  const [html, app] = await Promise.all([readHtml(), readApp()]);

  assert.match(html, /#choices\{gap:0;[\s\S]*?border-top:1px solid #cfd4d8/);
  assert.match(html, /#choices \.choice\{min-height:58px;[\s\S]*?border-radius:0/);
  assert.match(html, /#choices \.choice\{[\s\S]*?background:var\(--choice-color\);[\s\S]*?color:#fff/);
  assert.doesNotMatch(html, /#choices \.choice\{[^}]*linear-gradient/);
  assert.match(html, /#choices \.sw\{display:none\}/);
  assert.match(html, /#choices \.choice \.tag\{[\s\S]*?color:#fff/);
  assert.match(html, /#choices \.choice \.tag::after\{content:"→"/);
  assert.match(app, /b\.style\.setProperty\('--choice-color', hex\(c\.color\)\)/);
  assert.match(app, /nb\.style\.setProperty\('--choice-color', '#5a6675'\)/);
  assert.match(app, /color:rgba\(255,255,255,.82\)/);
  assert.match(app, /None of these are correct<\/span><span class="tag" data-tag><\/span>/);
});
