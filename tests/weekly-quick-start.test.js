import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';

const read = path => readFile(new URL(`../${path}`, import.meta.url), 'utf8');

function declaration(source, signature) {
  const start = source.indexOf(signature);
  assert.notEqual(start, -1, `expected ${signature}`);
  const open = source.indexOf('{', start + signature.length - 1);
  let depth = 0;
  for (let index = open; index < source.length; index += 1) {
    if (source[index] === '{') depth += 1;
    else if (source[index] === '}' && --depth === 0) return source.slice(start, index + 1);
  }
  throw new Error(`unbalanced declaration for ${signature}`);
}

test('weekly Quick start is accessible, concise, and does not imply inspection selects', async () => {
  const [html, app] = await Promise.all([read('index.html'), read('app.js')]);
  assert.match(html, /id="quick-start-open"[^>]*aria-haspopup="dialog"[^>]*aria-controls="quick-start-dialog"[^>]*hidden/);
  assert.match(html, /<dialog id="quick-start-dialog"[^>]*aria-labelledby="quick-start-title"[^>]*aria-describedby="quick-start-intro"/);
  assert.match(html, /Click or tap a ligand to zoom in; click white space to zoom out/);
  assert.match(html, /Drag to rotate, right-drag or Ctrl-drag to pan, and scroll or pinch to zoom/);
  assert.match(html, /<b>Show all<\/b> overlays poses, <b>One at a time<\/b> isolates them, and <b>Grid<\/b> compares them side by side/);
  assert.match(html, /<b>Select<\/b> your best pose and <b>Reject<\/b> any you rule out/);
  assert.match(html, /Surface and H-bonds are optional viewing aids/);
  assert.match(html, /<b>Record vote<\/b> saves each question immediately, even if you do not finish/);
  assert.match(html, /<b>Update vote<\/b> records a revision without erasing your earlier submission/);
  assert.match(html, /the arrows let you skip or revisit questions/);
  assert.doesNotMatch(html, /Inspection never reveals the reference structure or the answer/);
  assert.doesNotMatch(html, /Click or tap a ligand to (?:select|vote)/i);
  assert.match(app, /Click a ligand to zoom in; click white space to/);
  assert.match(app, /right-drag or Ctrl-drag to pan/);
  assert.doesNotMatch(app, /Inspect freely\. Select one pose; reject any you rule out\./);
});

test('weekly Quick start remains behind the persistent manual button', async () => {
  const app = await read('app.js');
  const events = [];
  const dialog = {
    open: false,
    showModal() { this.open = true; },
    setAttribute(name) { if (name === 'open') this.open = true; },
  };
  const context = vm.createContext({
    quizSource: 'weekly',
    $: selector => selector === '#quick-start-dialog' ? dialog : null,
    recordAppEvent: (action, state) => events.push([action, state]),
  });
  vm.runInContext(declaration(app, "function openWeeklyQuickStart(origin = 'manual')"), context);

  assert.equal(context.openWeeklyQuickStart('manual'), true);
  assert.equal(dialog.open, true, 'the persistent help button can reopen it');
  assert.equal(events[0][0], 'quick_start_opened');
  assert.equal(events[0][1].quick_start_origin, 'manual');
  assert.doesNotMatch(app, /maybeOpenWeeklyQuickStart/);
  assert.doesNotMatch(app, /sessionStorage/);
});

test('Quick start is exposed only while a weekly question is active', async () => {
  const app = await read('app.js');
  assert.match(app, /quickStart\.hidden = !visible/);
  assert.match(app, /const visible = !!cur && quizSource === 'weekly' && ITEMS\.length > 0/);
  assert.match(app, /startWeeklyThinkingTrace\(\);\s*const questionIndex/);
  assert.match(app, /recordAppEvent\('quick_start_closed'\)/);
});
