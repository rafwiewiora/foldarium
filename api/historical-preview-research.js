import { weeklyRetrospectivesConfig } from './weekly-retrospectives.js';
import {
  MAX_ARTIFACT_BYTES, historicalSummary, historicalArtifactLocation,
  verifyHistoricalArtifact,
} from '../lib/historical-preview-research.js';

const SUMMARY_FIELDS = 'round_id,campaign_id,environment,opens_at,closes_at,blind_manifest_sha256,scope_id,evaluation_id,public_artifact_sha256,public_artifact_size_bytes,item_count,choice_count,required_execution_ids,published_at,publication_scope,human_votes_included';
const PAGE_SIZE = 50;

async function boundedBytes(upstream, maximum) {
  if (!upstream.ok || Number(upstream.headers?.get('content-length')) > maximum) {
    await upstream.body?.cancel?.(); throw new Error('Unavailable');
  }
  if (!upstream.body?.getReader) {
    const value = Buffer.from(await upstream.arrayBuffer());
    if (value.length > maximum) throw new Error('Unavailable');
    return value;
  }
  const reader = upstream.body.getReader(), chunks = []; let length = 0;
  try {
    while (true) {
      const { done, value } = await reader.read(); if (done) break;
      length += value.length;
      if (length > maximum) { await reader.cancel(); throw new Error('Unavailable'); }
      chunks.push(value);
    }
  } finally { reader.releaseLock(); }
  return Buffer.concat(chunks, length);
}

export function createHistoricalPreviewResearchHandler({ env = process.env, fetchImpl = fetch } = {}) {
  return async function handler(request, response) {
    response.setHeader('Cache-Control', 'no-store');
    if (request.method !== 'GET') {
      response.setHeader('Allow', 'GET'); return response.status(405).json({ error: 'Method not allowed' });
    }
    const query = request.query || {}, roundId = query.round_id;
    if (Object.keys(query).some(k => !['round_id', 'offset'].includes(k))
      || (roundId != null && (typeof roundId !== 'string' || !/^[A-Za-z0-9_.-]{1,200}$/.test(roundId)))
      || (query.offset != null && (typeof query.offset !== 'string' || !/^(0|[1-9][0-9]{0,3})$/.test(query.offset) || Number(query.offset) > 1000))
      || (roundId != null && query.offset != null)) return response.status(400).json({ error: 'Invalid request' });
    const config = weeklyRetrospectivesConfig(env);
    if (!config.url || !config.serviceRoleKey) return response.status(503).json({ error: 'Historical research unavailable' });
    const headers = { apikey: config.serviceRoleKey };
    if (!config.serviceRoleKey.startsWith('sb_secret_')) headers.Authorization = `Bearer ${config.serviceRoleKey}`;
    const json = async (url, init = {}) => JSON.parse((await boundedBytes(await fetchImpl(url, { ...init, headers: { ...headers, ...init.headers }, signal: AbortSignal.timeout(20000) }), 1024 * 1024)).toString('utf8'));
    try {
      const offset = Number(query.offset || 0);
      const params = new URLSearchParams({ select: SUMMARY_FIELDS, order: 'published_at.desc,round_id.desc', limit: String(roundId ? 2 : PAGE_SIZE + 1) });
      if (roundId) params.set('round_id', `eq.${roundId}`); else params.set('offset', String(offset));
      const rows = await json(`${config.url}/rest/v1/public_weekly_historical_preview_research?${params}`);
      if (!Array.isArray(rows) || rows.length > (roundId ? 2 : PAGE_SIZE + 1)) throw new Error('Unavailable');
      if (!roundId) {
        const items = rows.slice(0, PAGE_SIZE).map(historicalSummary);
        response.setHeader('Cache-Control', 'public, max-age=60, s-maxage=300');
        return response.status(200).json({ format_version: 'foldarium.historical-preview-research-list/v1', human_votes_included: false, items, next_offset: rows.length > PAGE_SIZE ? offset + PAGE_SIZE : null });
      }
      if (rows.length !== 1 || rows[0].round_id !== roundId) throw new Error('Unavailable');
      const summary = rows[0];
      const catalog = await json(`${config.url}/rest/v1/rpc/get_weekly_historical_preview_v1`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ p_round_id: roundId }) });
      const location = historicalArtifactLocation(catalog, summary);
      const bytes = await boundedBytes(await fetchImpl(`${config.url}/storage/v1/object/authenticated/${encodeURIComponent(location.bucket)}/${location.objectPath}`, { headers, signal: AbortSignal.timeout(20000) }), MAX_ARTIFACT_BYTES);
      const artifact = verifyHistoricalArtifact(bytes, catalog, summary);
      const etag = `"historical-preview-v1-${summary.public_artifact_sha256}"`;
      response.setHeader('ETag', etag);
      // Short cache: original source eligibility is checked on every origin read.
      response.setHeader('Cache-Control', 'public, max-age=60, s-maxage=300');
      if (request.headers?.['if-none-match'] === etag) return response.status(304).end();
      return response.status(200).json(artifact);
    } catch {
      response.setHeader('Cache-Control', 'no-store');
      return response.status(404).json({ error: 'Not found' });
    }
  };
}

export default createHistoricalPreviewResearchHandler();
