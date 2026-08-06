import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

test('benchmark demo resolves assets from its canonical directory', async () => {
  const html = await readFile(new URL('../benchmark/demo/index.html', import.meta.url), 'utf8');
  const baseHref = html.match(/<base href="([^"]+)"\/?>/)?.[1];

  assert.equal(baseHref, '/benchmark/demo/');
  assert.equal(
    new URL('./app.js', `https://www.foldarium.org${baseHref}`).pathname,
    '/benchmark/demo/app.js',
  );
  assert.equal(
    new URL('./systems.json', `https://www.foldarium.org${baseHref}`).pathname,
    '/benchmark/demo/systems.json',
  );
});
