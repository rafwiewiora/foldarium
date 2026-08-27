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

test('Grid ends above the measured floating controls instead of scrolling behind them', async () => {
  const html = await readHtml();
  const app = await readApp();

  assert.match(html, /#stage\{--grid-controls-clearance:96px/);
  assert.match(html, /#gridview\{[^}]*padding:10px;/);
  assert.match(html, /#gridview\.on\{[^}]*bottom:var\(--grid-controls-clearance\)/);
  assert.match(app, /function reserveGridControlClearance\(\)/);
  assert.match(app, /height \+ 28/);
  assert.match(app, /observer\.observe\(\$\('#view-options'\)\)/);
});

test('viewer controls form an always-visible bottom toolbar', async () => {
  const html = await readHtml();

  assert.match(html, /#view-options\{[\s\S]*?display:flex;[\s\S]*?background:transparent;[\s\S]*?border:0/);
  assert.doesNotMatch(html, /<summary>View options<\/summary>/);
  assert.match(html, /#view-options \.control-group\{display:flex/);
  assert.match(html, /<span class="control-label">Layout<\/span>/);
  assert.match(html, /<span class="control-label">View<\/span>/);
});

test('One at a time exposes review controls during voting and retrospective review', async () => {
  const [html, app] = await Promise.all([readHtml(), readApp()]);

  assert.match(html, /id="one-review-actions" hidden/);
  assert.match(html, /id="one-select"[^>]*>Select<\/button>/);
  assert.match(html, /id="one-reject"[^>]*>Reject<\/button>/);
  assert.match(app, /const retrospective = !!choice && retrospectiveAnswerActive\(\)/);
  assert.match(app, /const visible = !!choice && cur\.item\.source === 'weekly'[\s\S]*&& \(!cur\.revealed \|\| retrospective\)[\s\S]*&& !\(retrospective && isArchiveRetrospective\(\)\)/);
  assert.match(app, /if \(!cur \|\| displayMode !== 'one'\) return null/);
  assert.match(app, /Click a ligand to zoom in; click white space to/);
  assert.match(app, /setVoteStatus\('Recording…', 'recording'\)/);
  assert.match(app, /'Vote saved\.', 'saved'/);
  assert.match(html, /\.verdict\[data-state="recording"\]/);
  assert.match(html, /\.verdict\[data-state="saved"\]/);
  assert.match(html, /\.grid-card\.rejected,#app\.rejected\{opacity:\.32;filter:grayscale\(\.8\)\}/);
  assert.match(app, /\$\('#app'\)\?\.classList\.toggle\('rejected', rejected\)/);
});

test('question context is arranged at the top of the viewer', async () => {
  const html = await readHtml();
  const stage = html.indexOf('<div id="stage">');
  const context = html.indexOf('<div id="viewer-question">');
  const ligand = html.indexOf('id="ligand"', context);
  const instruction = html.indexOf('id="instruction"', context);

  assert.ok(stage < context && context < ligand && ligand < instruction);
  assert.match(html, /#stage-topbar\{position:absolute;[\s\S]*?top:14px/);
  assert.match(html, /#viewer-question\{position:static;[\s\S]*?flex:1 1 620px/);
  assert.match(html, /max-width:760px/);
  assert.match(html, /#stage-topbar\{[\s\S]*?right:64px/);
  assert.match(html, /#gridview\.on\{display:block;top:var\(--grid-top-clearance\);bottom:var\(--grid-controls-clearance\)\}/);
});

test('active-pose badge is legible and clears the Molstar reset control', async () => {
  const html = await readHtml();

  assert.match(html, /\.badge\{position:static;[\s\S]*?font-size:13px;[\s\S]*?padding:7px 12px/);
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

test('Foldarium branding is present and the name intro starts centered', async () => {
  const html = await readHtml();
  const logo = await readFile(new URL('../assets/foldarium-mark.svg', import.meta.url), 'utf8');

  assert.match(html, /<link rel="icon" type="image\/svg\+xml" href="\/assets\/foldarium-mark\.svg\?v=\d+"/);
  assert.match(html, /<img class="brand-mark" src="\/assets\/foldarium-mark\.svg\?v=\d+" alt=""/);
  assert.match(html, /<h1>Foldarium<\/h1>/);
  assert.match(html, /<div id="wrap" class="intro" hidden>/);
  assert.match(html, /#wrap\.intro #side\{width:100%;max-width:480px;margin:auto/);
  assert.match(html, /#wrap\.intro #question-head,[\s\S]*?#wrap\.intro #answer-details\{display:none!important\}/);
  assert.doesNotMatch(html, /html\[data-quiz-mode="weekly"\] #instruction\{display:none!important\}/);
  for (const color of ['#5b8ff9', '#f6bd16', '#9270ca', '#5ad8a6']) {
    assert.match(logo.toLowerCase(), new RegExp(color));
  }
  assert.doesNotMatch(logo, /#111820/i);
});

test('left pose selections use calm cards with pose-color rails', async () => {
  const [html, app] = await Promise.all([readHtml(), readApp()]);

  assert.match(html, /#choices\{gap:8px;background:transparent\}/);
  assert.match(html, /#choices \.choice\{min-height:50px;[\s\S]*?border-left:5px solid var\(--choice-color\);[\s\S]*?border-radius:9px;[\s\S]*?background:#fff;color:var\(--ink\)/);
  assert.doesNotMatch(html, /#choices \.choice\{[^}]*background:var\(--choice-color\)/);
  assert.match(html, /#choices \.sw\{display:none\}/);
  assert.match(html, /#choices \.pose-count\{[\s\S]*?color:var\(--muted\)/);
  assert.doesNotMatch(html, /#choices \.choice \.tag::after/);
  assert.match(app, /b\.style\.setProperty\('--choice-color', hex\(c\.color\)\)/);
  assert.match(app, /nb\.style\.setProperty\('--choice-color', '#5a6675'\)/);
  assert.match(app, /class="pose-count"/);
  assert.match(app, /None are correct<\/span><span class="tag" data-tag><\/span>/);
});

test('weekly entry hides irrelevant setup and uses light research controls', async () => {
  const html = await readHtml();

  assert.match(html, /html\[data-quiz-mode="weekly"\] #setup,[\s\S]*?#score-summary\{display:none!important\}/);
  assert.match(html, /\.participant-setup\{[\s\S]*?background:#f6f8fa\}/);
  assert.match(html, /\.dialog-form input,\.dialog-form textarea\{[\s\S]*?background:#fff/);
  assert.match(html, /\.privacy-note\{[\s\S]*?background:#f6f8fa/);
});

test('Submit answer matches the bottom viewer controls', async () => {
  const html = await readHtml();

  assert.match(html, /#view-options button,#lock\{[\s\S]*?background:#fff;[\s\S]*?color:var\(--muted\);[\s\S]*?border:1px solid var\(--line\);[\s\S]*?font-size:12\.5px/);
});

test('Weekly Record vote is the prominent primary viewer action', async () => {
  const html = await readHtml();

  assert.match(html, /html\[data-quiz-mode="weekly"\] #lock\{[\s\S]*?min-height:44px;[\s\S]*?background:var\(--accent\);[\s\S]*?color:#fff;[\s\S]*?font-weight:700/);
  assert.match(html, /html\[data-quiz-mode="weekly"\] #lock:disabled\{background:#dce7ec/);
});

test('Grid mode does not expose the hidden canonical viewer', async () => {
  const [html, app] = await Promise.all([readHtml(), readApp()]);

  assert.match(html, /#stage\.grid-active #app\{visibility:hidden\}/);
  assert.match(app, /\$\('#stage'\)\.classList\.add\('grid-active'\)/);
  assert.match(app, /\$\('#stage'\)\.classList\.remove\('grid-active'\)/);
});
