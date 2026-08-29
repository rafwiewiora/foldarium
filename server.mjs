import { createServer as createHttpServer } from 'node:http';
import { createReadStream } from 'node:fs';
import { realpath, stat } from 'node:fs/promises';
import { dirname, extname, isAbsolute, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import configHandler from './api/config.js';
import privateEvaluationHandler from './api/private-evaluation.js';
import replayHandler from './api/replay.js';
import weeklyPlayForFunResultsHandler from './api/weekly-play-for-fun-results.js';
import weeklyResultsHandler from './api/weekly-results.js';
import weeklyRetrospectivesHandler from './api/weekly-retrospectives.js';
import weeklySelectorResultsHandler from './api/weekly-selector-results.js';
import weeklySelectorHandler from './api/weekly-selector.js';

const PROJECT_ROOT = dirname(fileURLToPath(import.meta.url));

export const DEFAULT_BODY_LIMIT_BYTES = 1024 * 1024;

export const DEFAULT_API_HANDLERS = Object.freeze({
  config: configHandler,
  'private-evaluation': privateEvaluationHandler,
  replay: replayHandler,
  'weekly-play-for-fun-results': weeklyPlayForFunResultsHandler,
  'weekly-results': weeklyResultsHandler,
  'weekly-retrospectives': weeklyRetrospectivesHandler,
  'weekly-selector-results': weeklySelectorResultsHandler,
  'weekly-selector': weeklySelectorHandler,
});

const CONTENT_TYPES = Object.freeze({
  '.avif': 'image/avif',
  '.cif': 'chemical/x-cif',
  '.css': 'text/css; charset=utf-8',
  '.csv': 'text/csv; charset=utf-8',
  '.gif': 'image/gif',
  '.html': 'text/html; charset=utf-8',
  '.ico': 'image/x-icon',
  '.jpeg': 'image/jpeg',
  '.jpg': 'image/jpeg',
  '.js': 'text/javascript; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.map': 'application/json; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8',
  '.pdb': 'chemical/x-pdb',
  '.png': 'image/png',
  '.svg': 'image/svg+xml',
  '.txt': 'text/plain; charset=utf-8',
  '.wasm': 'application/wasm',
  '.webp': 'image/webp',
  '.woff': 'font/woff',
  '.woff2': 'font/woff2',
  '.xml': 'application/xml; charset=utf-8',
  '.zip': 'application/zip',
});

export function resolveServerConfig(env = process.env) {
  const host = typeof env.HOST === 'string' && env.HOST.trim()
    ? env.HOST.trim()
    : '127.0.0.1';
  const rawPort = env.PORT == null || env.PORT === '' ? '4319' : String(env.PORT);
  const port = Number(rawPort);
  if (!Number.isSafeInteger(port) || port < 0 || port > 65535 || String(port) !== rawPort) {
    throw new TypeError('PORT must be an integer from 0 through 65535');
  }
  return { host, port };
}

export function createRequestListener({
  rootDirectory = PROJECT_ROOT,
  apiHandlers = DEFAULT_API_HANDLERS,
  bodyLimitBytes = DEFAULT_BODY_LIMIT_BYTES,
  logger = console,
} = {}) {
  if (!Number.isSafeInteger(bodyLimitBytes) || bodyLimitBytes < 1) {
    throw new TypeError('bodyLimitBytes must be a positive integer');
  }

  const root = resolve(rootDirectory);
  const realRootPromise = realpath(root);

  return async function requestListener(request, response) {
    let url;
    try {
      url = new URL(request.url || '/', 'http://foldarium.local');
    } catch {
      return sendText(response, 400, 'Bad request');
    }

    try {
      if (url.pathname === '/api' || url.pathname.startsWith('/api/')) {
        await dispatchApi({
          request,
          response,
          url,
          apiHandlers,
          bodyLimitBytes,
        });
        return;
      }

      await serveStatic({
        request,
        response,
        pathname: mappedStaticPath(url.pathname),
        root,
        realRoot: await realRootPromise,
      });
    } catch (error) {
      if (response.writableEnded) return;
      if (response.headersSent) {
        response.destroy(error);
        return;
      }
      if (error?.code === 'BODY_TOO_LARGE') {
        return sendJson(response, 413, { error: 'Request body is too large' });
      }
      if (error?.code === 'INVALID_BODY') {
        return sendJson(response, 400, { error: 'Invalid request body' });
      }
      if (error?.code === 'REQUEST_ABORTED') return;
      logger?.error?.(error);
      return sendText(response, 500, 'Internal server error');
    }
  };
}

export function createFoldariumServer(options = {}) {
  return createHttpServer(createRequestListener(options));
}

async function dispatchApi({
  request,
  response,
  url,
  apiHandlers,
  bodyLimitBytes,
}) {
  const handlerName = apiHandlerName(url.pathname);
  const handler = handlerName && (
    apiHandlers instanceof Map ? apiHandlers.get(handlerName) : apiHandlers[handlerName]
  );
  if (typeof handler !== 'function') {
    sendJson(response, 404, { error: 'Not found' });
    return;
  }

  request.query = searchParamsObject(url.searchParams);
  request.body = await readRequestBody(request, bodyLimitBytes);
  addResponseCompatibility(response);
  await handler(request, response);
}

function apiHandlerName(pathname) {
  if (/^\/api\/weekly-selector(?:\/|$)/.test(pathname)) return 'weekly-selector';
  const match = /^\/api\/([a-z0-9-]+)\/?$/.exec(pathname);
  return match?.[1] || '';
}

function searchParamsObject(searchParams) {
  const query = Object.create(null);
  for (const [name, value] of searchParams) {
    const previous = query[name];
    if (previous === undefined) query[name] = value;
    else if (Array.isArray(previous)) previous.push(value);
    else query[name] = [previous, value];
  }
  return query;
}

async function readRequestBody(request, limit) {
  if (request.method === 'GET' || request.method === 'HEAD') return undefined;

  const contentLength = request.headers['content-length'];
  if (contentLength != null) {
    const declaredLength = Number(contentLength);
    if (Number.isFinite(declaredLength) && declaredLength > limit) {
      request.resume();
      throw requestError('BODY_TOO_LARGE');
    }
  }

  const chunks = [];
  let length = 0;
  for await (const chunk of request) {
    length += chunk.length;
    if (length > limit) {
      request.resume();
      throw requestError('BODY_TOO_LARGE');
    }
    chunks.push(chunk);
  }
  if (!request.complete) throw requestError('REQUEST_ABORTED');
  if (length === 0) return undefined;

  const rawBody = Buffer.concat(chunks, length);
  const contentType = String(request.headers['content-type'] || '')
    .split(';', 1)[0]
    .trim()
    .toLowerCase();
  if (contentType === 'application/json' || contentType.endsWith('+json')) {
    try {
      return JSON.parse(rawBody.toString('utf8'));
    } catch {
      throw requestError('INVALID_BODY');
    }
  }
  if (contentType === 'application/x-www-form-urlencoded') {
    return searchParamsObject(new URLSearchParams(rawBody.toString('utf8')));
  }
  return rawBody.toString('utf8');
}

function addResponseCompatibility(response) {
  response.status = function status(statusCode) {
    this.statusCode = statusCode;
    return this;
  };
  response.json = function json(value) {
    if (!this.hasHeader('Content-Type')) {
      this.setHeader('Content-Type', 'application/json; charset=utf-8');
    }
    this.end(JSON.stringify(value));
    return this;
  };
  response.send = function send(value = '') {
    if (value != null && typeof value === 'object' && !Buffer.isBuffer(value)) {
      return this.json(value);
    }
    this.end(value);
    return this;
  };
}

function mappedStaticPath(pathname) {
  if (pathname === '/weekly' || pathname === '/weekly/' || pathname === '/weekly.html') {
    return '/index.html';
  }
  if (/^\/weekly\/retrospectives(?:\/[^/]+)?\/?$/.test(pathname)) {
    return '/weekly-retrospectives.html';
  }
  return pathname;
}

async function serveStatic({ request, response, pathname, root, realRoot }) {
  if (request.method !== 'GET' && request.method !== 'HEAD') {
    response.setHeader('Allow', 'GET, HEAD');
    sendText(response, 405, 'Method not allowed');
    return;
  }

  let decodedPath;
  try {
    decodedPath = decodeURIComponent(pathname);
  } catch {
    sendText(response, 400, 'Bad request');
    return;
  }
  if (decodedPath.includes('\0') || decodedPath.includes('\\')) {
    sendText(response, 400, 'Bad request');
    return;
  }

  const segments = decodedPath.split('/').filter(Boolean);
  if (segments.some(segment => segment.startsWith('.'))) {
    sendText(response, 404, 'Not found');
    return;
  }

  let candidate = resolve(root, decodedPath.replace(/^\/+/, '') || 'index.html');
  if (!isWithin(root, candidate)) {
    sendText(response, 404, 'Not found');
    return;
  }

  let fileStat;
  try {
    fileStat = await stat(candidate);
    if (fileStat.isDirectory()) {
      candidate = resolve(candidate, 'index.html');
      fileStat = await stat(candidate);
    }
    if (!fileStat.isFile()) throw Object.assign(new Error('not a file'), { code: 'ENOENT' });
    candidate = await realpath(candidate);
  } catch (error) {
    if (error?.code === 'ENOENT' || error?.code === 'ENOTDIR' || error?.code === 'EACCES') {
      sendText(response, 404, 'Not found');
      return;
    }
    throw error;
  }

  if (!isWithin(realRoot, candidate)) {
    sendText(response, 404, 'Not found');
    return;
  }

  response.statusCode = 200;
  response.setHeader('Content-Type', CONTENT_TYPES[extname(candidate).toLowerCase()]
    || 'application/octet-stream');
  response.setHeader('Content-Length', fileStat.size);
  response.setHeader('X-Content-Type-Options', 'nosniff');
  if (request.method === 'HEAD') {
    response.end();
    return;
  }

  await new Promise((resolveStream, rejectStream) => {
    const stream = createReadStream(candidate);
    stream.on('error', rejectStream);
    stream.on('end', resolveStream);
    response.on('close', resolveStream);
    stream.pipe(response);
  });
}

function isWithin(root, candidate) {
  const pathFromRoot = relative(root, candidate);
  return pathFromRoot === '' || (!pathFromRoot.startsWith('..') && !isAbsolute(pathFromRoot));
}

function requestError(code) {
  return Object.assign(new Error(code), { code });
}

function sendJson(response, statusCode, value) {
  response.statusCode = statusCode;
  response.setHeader('Content-Type', 'application/json; charset=utf-8');
  response.end(JSON.stringify(value));
}

function sendText(response, statusCode, value) {
  response.statusCode = statusCode;
  response.setHeader('Content-Type', 'text/plain; charset=utf-8');
  response.end(value);
}

function isMainModule() {
  if (!process.argv[1]) return false;
  return resolve(process.argv[1]) === fileURLToPath(import.meta.url);
}

if (isMainModule()) {
  const { host, port } = resolveServerConfig();
  const server = createFoldariumServer();
  server.on('error', error => {
    console.error(error);
    process.exitCode = 1;
  });
  server.listen(port, host, () => {
    const address = server.address();
    const listeningPort = typeof address === 'object' && address ? address.port : port;
    console.log(`Foldarium listening on http://${host}:${listeningPort}`);
  });
}
