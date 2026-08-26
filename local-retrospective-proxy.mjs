import http from 'node:http';
import { readFileSync } from 'node:fs';
import { upgradeLocalPrivateReviewBundle } from './local-private-review-bundle.js';

const port = Number(process.env.PORT || 4319);
const upstream = new URL(process.env.UPSTREAM_URL || 'http://127.0.0.1:4317');
const bundlePath = process.env.PRIVATE_BUNDLE_PATH;

if (!bundlePath) {
  throw new Error('PRIVATE_BUNDLE_PATH is required');
}

const output = readFileSync(bundlePath, 'utf8');
const marker = '{"format_version":"foldarium.weekly-private-evaluation/';
const bundleStart = output.indexOf(marker);
if (bundleStart < 0) {
  throw new Error('Private evaluation bundle was not found');
}
const bundle = upgradeLocalPrivateReviewBundle(
  JSON.parse(output.slice(bundleStart).trim()),
);
const bundleBody = JSON.stringify(bundle);

const browserConfig = JSON.stringify({
  url: 'https://wwentnogbknrbmxhfgbg.supabase.co',
  publishableKey: 'sb_publishable_JvyIZVDB2l6t7zIRpBBo7Q_FdHdD36v',
  structureBaseUrl: 'https://wwentnogbknrbmxhfgbg.supabase.co/storage/v1/object/public/structures',
  enabled: true,
  writable: false,
  deploymentEnvironment: 'preview',
  commitSha: 'local-retrospective',
});

function json(response, body) {
  response.writeHead(200, {
    'content-type': 'application/json; charset=utf-8',
    'cache-control': 'no-store',
  });
  response.end(body);
}

const server = http.createServer((request, response) => {
  const requestUrl = new URL(request.url || '/', upstream);
  if (requestUrl.pathname === '/api/config') {
    json(response, browserConfig);
    return;
  }
  if (requestUrl.pathname === '/api/private-evaluation') {
    if (request.method !== 'POST') {
      response.writeHead(405, { allow: 'POST' });
      response.end();
      return;
    }
    json(response, bundleBody);
    return;
  }

  const proxy = http.request(requestUrl, {
    method: request.method,
    headers: {
      ...request.headers,
      host: upstream.host,
    },
  }, upstreamResponse => {
    response.writeHead(upstreamResponse.statusCode || 502, upstreamResponse.headers);
    upstreamResponse.pipe(response);
  });
  proxy.on('error', error => {
    response.writeHead(502, { 'content-type': 'text/plain; charset=utf-8' });
    response.end(`Local upstream unavailable: ${error.message}`);
  });
  request.pipe(proxy);
});

server.listen(port, '127.0.0.1', () => {
  console.log(`Local retrospective ready at http://127.0.0.1:${port}/weekly`);
});
