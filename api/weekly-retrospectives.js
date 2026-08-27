import {
  ARCHIVE_LIST_FORMAT_VERSION,
  EVALUATION_SELECT_FIELDS,
  PARTICIPANT_KINDS,
  PUBLICATION_SELECT_FIELDS,
  RANKING_VIEWS,
  ROUND_SELECT_FIELDS,
  WeeklyRetrospectiveError,
  buildAdminAllTime,
  buildAdminDetail,
  buildPublicDetail,
  buildPublicHumanAllTime,
  decodeArchiveCursor,
  encodeArchiveCursor,
  parseSupabaseObjectUri,
  publicationSummary,
  publishHumanPseudonyms,
  verifyAdminArtifact,
  verifyEvaluationAndRound,
  verifyPublicationCatalogRow,
  verifyPublicArtifact,
  verifySourceSnapshot,
} from '../lib/weekly-retrospectives.js';

const PUBLIC_BRIEF_CACHE = 'public, max-age=60, s-maxage=300, stale-while-revalidate=600';
const PUBLIC_DETAIL_CACHE = 'public, max-age=300, s-maxage=86400, stale-while-revalidate=604800';
const NO_STORE = 'no-store';
export const ARTIFACT_LOAD_CONCURRENCY = 5;

export function createWeeklyRetrospectivesHandler({
  env = process.env,
  fetchImpl = fetch,
} = {}) {
  return async function handler(request, response) {
    response.setHeader('Cache-Control', NO_STORE);
    if (request.method !== 'GET') {
      response.setHeader('Allow', 'GET');
      return send(response, 405, { error: 'Method not allowed' });
    }
    if (request.query?.admin === '1' && !adminEnabled(env)) {
      return send(response, 404, { error: 'Not found' });
    }

    let mode;
    try {
      mode = parseMode(request.query || {});
    } catch (error) {
      return send(response, 400, { error: 'Invalid request' });
    }
    const config = weeklyRetrospectivesConfig(env);
    if (!config.url || !config.serviceRoleKey) {
      return send(response, 503, { error: 'Weekly retrospectives unavailable' });
    }
    const client = createArchiveClient(config, fetchImpl);

    try {
      if (mode.name === 'list') {
        const result = await listPublications(client, mode);
        response.setHeader('Cache-Control', PUBLIC_BRIEF_CACHE);
        return send(response, 200, result);
      }
      if (mode.name === 'detail') {
        const publication = await client.fetchPublication(mode.roundId);
        const [context, artifactBytes, adminBytes] = await Promise.all([
          client.fetchVerifiedContext(publication),
          client.download(publication.descriptors[
            mode.admin ? 'admin_artifact' : 'public_artifact'
          ]),
          mode.admin
            ? Promise.resolve(null)
            : client.download(publication.descriptors.admin_artifact),
        ]);
        if (mode.admin) {
          const adminArtifact = verifyAdminArtifact(artifactBytes, publication);
          return send(response, 200, buildAdminDetail({
            publication,
            context,
            adminArtifact,
          }));
        }
        const publicArtifact = publishHumanPseudonyms({
          publication,
          publicArtifact: verifyPublicArtifact(artifactBytes, publication),
          adminArtifact: verifyAdminArtifact(adminBytes, publication),
        });
        const etag = `"weekly-retrospective-names-v1-${publication.digests.admin_artifact_sha256}"`;
        response.setHeader('ETag', etag);
        response.setHeader('Cache-Control', PUBLIC_DETAIL_CACHE);
        if (etagMatches(request.headers, etag)) return notModified(response);
        return send(response, 200, buildPublicDetail({
          publication,
          context,
          publicArtifact,
        }));
      }

      const publications = await client.fetchAllPublications();
      if (mode.admin) {
        const hmacKey = participantHmacKey(env);
        const weeks = await mapWithConcurrency(
          publications,
          ARTIFACT_LOAD_CONCURRENCY,
          async publication => {
          const [context, sourceBytes, publicBytes] = await Promise.all([
            client.fetchVerifiedContext(publication),
            client.download(publication.descriptors.source_snapshot),
            client.download(publication.descriptors.public_artifact),
          ]);
          return {
            publication,
            context,
            sourceSnapshot: verifySourceSnapshot(sourceBytes, publication),
            publicArtifact: verifyPublicArtifact(publicBytes, publication),
          };
          },
        );
        return send(response, 200, buildAdminAllTime(weeks, {
          hmacKey,
          ranking: mode.ranking,
          participantKind: mode.participantKind,
        }));
      }
      const weeks = await mapWithConcurrency(
        publications,
        ARTIFACT_LOAD_CONCURRENCY,
        async publication => {
        const [context, sourceBytes, publicBytes] = await Promise.all([
          client.fetchVerifiedContext(publication),
          client.download(publication.descriptors.source_snapshot),
          client.download(publication.descriptors.public_artifact),
        ]);
        return {
          publication,
          context,
          sourceSnapshot: verifySourceSnapshot(sourceBytes, publication),
          publicArtifact: verifyPublicArtifact(publicBytes, publication),
        };
        },
      );
      const result = buildPublicHumanAllTime(weeks, {
        ranking: mode.ranking,
        participantKind: mode.participantKind,
      });
      response.setHeader('Cache-Control', PUBLIC_BRIEF_CACHE);
      return send(response, 200, result);
    } catch (error) {
      response.setHeader('Cache-Control', NO_STORE);
      if (error instanceof WeeklyRetrospectiveError) {
        return send(response, 404, { error: 'Not found' });
      }
      return send(response, 404, { error: 'Not found' });
    }
  };
}

export function weeklyRetrospectivesConfig(env) {
  return {
    url: normalizedOrigin(
      env.FOLDARIUM_PRODUCTION_SUPABASE_URL || env.SUPABASE_URL,
    ),
    serviceRoleKey: normalizedServiceRoleKey(
      env.FOLDARIUM_PRODUCTION_SUPABASE_SERVICE_ROLE_KEY
        || env.SUPABASE_SERVICE_ROLE_KEY,
    ),
  };
}

export function adminEnabled(env) {
  return env.FOLDARIUM_ENV === 'preview'
    && env.FOLDARIUM_WEEKLY_RETROSPECTIVE_ADMIN_ENABLED === '1'
    && env.FOLDARIUM_WEEKLY_RETROSPECTIVE_ADMIN_ACCESS === 'authenticated-proxy';
}

function participantHmacKey(env) {
  const value = env.FOLDARIUM_WEEKLY_RETROSPECTIVE_PARTICIPANT_HMAC_KEY;
  if (typeof value !== 'string' || value.length < 32 || value.length > 4096
    || /[\u0000-\u0020\u007f]/.test(value)) {
    throw new WeeklyRetrospectiveError('participant HMAC key is unavailable');
  }
  return value;
}

function parseMode(query) {
  const admin = optionalFlag(query.admin, 'admin');
  const allTime = optionalFlag(query.all_time, 'all_time');
  const roundId = optionalSingle(query.round_id, 'round_id');
  const cursorRaw = optionalSingle(query.cursor, 'cursor');
  const limitRaw = optionalSingle(query.limit, 'limit');
  const ranking = optionalSingle(query.ranking, 'ranking') || 'total_correct';
  const participantKind = optionalSingle(query.participant_kind, 'participant_kind');

  if (allTime && roundId || admin && !allTime && !roundId) {
    throw new WeeklyRetrospectiveError('request mode is invalid');
  }
  if (roundId && (!/^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$/.test(roundId)
    || cursorRaw != null || limitRaw != null
    || query.ranking != null || query.participant_kind != null)) {
    throw new WeeklyRetrospectiveError('detail request is invalid');
  }
  if (allTime && (cursorRaw != null || limitRaw != null)) {
    throw new WeeklyRetrospectiveError('all-time request is invalid');
  }
  if (!allTime && !roundId && (admin || query.ranking != null
    || query.participant_kind != null)) {
    throw new WeeklyRetrospectiveError('list request is invalid');
  }
  if (allTime && (!RANKING_VIEWS.includes(ranking)
    || (participantKind != null && !PARTICIPANT_KINDS.includes(participantKind)))) {
    throw new WeeklyRetrospectiveError('ranking request is invalid');
  }
  if (roundId) return { name: 'detail', admin, roundId };
  if (allTime) {
    return {
      name: 'all-time',
      admin,
      ranking,
      participantKind,
    };
  }

  const limit = limitRaw == null ? 20 : Number(limitRaw);
  if (!Number.isSafeInteger(limit) || limit < 1 || limit > 50
    || String(limit) !== String(limitRaw ?? limit)) {
    throw new WeeklyRetrospectiveError('list limit is invalid');
  }
  return {
    name: 'list',
    admin: false,
    limit,
    cursor: cursorRaw == null ? null : decodeArchiveCursor(cursorRaw),
  };
}

async function listPublications(client, mode) {
  const rows = await client.fetchPublicationPage(mode);
  const hasNext = rows.length > mode.limit;
  const page = rows.slice(0, mode.limit);
  const publications = page.map(verifyPublicationCatalogRow);
  const summaries = await mapWithConcurrency(
    publications,
    ARTIFACT_LOAD_CONCURRENCY,
    async publication => {
      const [context, publicBytes, adminBytes] = await Promise.all([
        client.fetchVerifiedContext(publication),
        client.download(publication.descriptors.public_artifact),
        client.download(publication.descriptors.admin_artifact),
      ]);
      const publicArtifact = publishHumanPseudonyms({
        publication,
        publicArtifact: verifyPublicArtifact(publicBytes, publication),
        adminArtifact: verifyAdminArtifact(adminBytes, publication),
      });
      return publicationSummary(publication, {
        context,
        publicArtifact,
      });
    },
  );
  const last = publications.at(-1);
  return {
    format_version: ARCHIVE_LIST_FORMAT_VERSION,
    publications: summaries,
    next_cursor: hasNext && last ? encodeArchiveCursor({
      revealedAt: last.revealedAt,
      roundId: last.roundId,
    }) : null,
  };
}

export async function mapWithConcurrency(values, maxConcurrency, mapper) {
  if (!Array.isArray(values) || !Number.isSafeInteger(maxConcurrency)
    || maxConcurrency < 1 || typeof mapper !== 'function') {
    throw new TypeError('bounded concurrency arguments are invalid');
  }
  const results = new Array(values.length);
  let nextIndex = 0;
  const workers = Array.from(
    { length: Math.min(maxConcurrency, values.length) },
    async () => {
      while (nextIndex < values.length) {
        const index = nextIndex;
        nextIndex += 1;
        results[index] = await mapper(values[index], index);
      }
    },
  );
  await Promise.all(workers);
  return results;
}

function createArchiveClient(config, fetchImpl) {
  const headers = serviceHeaders(config.serviceRoleKey);
  const fetchRows = async (table, params, range = null) => {
    const query = new URLSearchParams(params);
    const upstream = await fetchImpl(
      `${config.url}/rest/v1/${table}?${query.toString()}`,
      { headers: range ? { ...headers, Range: range } : headers },
    );
    if (!upstream.ok) throw new WeeklyRetrospectiveError('catalog request failed');
    const rows = await upstream.json();
    if (!Array.isArray(rows)) {
      throw new WeeklyRetrospectiveError('catalog response is invalid');
    }
    return rows;
  };
  const one = async (table, params) => {
    const rows = await fetchRows(table, { ...params, limit: '2' });
    if (rows.length !== 1) throw new WeeklyRetrospectiveError('catalog row is unavailable');
    return rows[0];
  };
  const download = async descriptor => {
    const upstream = await fetchImpl(
      `${config.url}/storage/v1/object/authenticated/`
        + `${encodeURIComponent(descriptor.bucket)}/${descriptor.objectPath}`,
      { headers },
    );
    if (!upstream.ok) throw new WeeklyRetrospectiveError('artifact download failed');
    return Buffer.from(await upstream.arrayBuffer());
  };
  const fetchVerifiedContext = async publication => {
    const [evaluationRow, roundRow] = await Promise.all([
      one('weekly_quiz_evaluations', {
        select: EVALUATION_SELECT_FIELDS,
        evaluation_id: `eq.${publication.evaluationId}`,
      }),
      one('weekly_quiz_rounds', {
        select: ROUND_SELECT_FIELDS,
        round_id: `eq.${publication.roundId}`,
      }),
    ]);
    const evaluationLocation = parseSupabaseObjectUri(
      evaluationRow.artifact_object_uri,
      publication.digests.evaluation_artifact_sha256,
    );
    const evaluationBytes = await download({
      ...evaluationLocation,
      objectUri: evaluationRow.artifact_object_uri,
      sha256: publication.digests.evaluation_artifact_sha256,
      sizeBytes: evaluationRow.artifact_size_bytes,
      mediaType: evaluationRow.artifact_media_type,
    });
    return verifyEvaluationAndRound({
      publication,
      evaluationRow,
      evaluationBytes,
      roundRow,
      assetOrigin: config.url,
    });
  };
  return {
    async fetchPublication(roundId) {
      return verifyPublicationCatalogRow(await one(
        'weekly_retrospective_publications',
        {
          select: PUBLICATION_SELECT_FIELDS,
          round_id: `eq.${roundId}`,
        },
      ));
    },
    async fetchPublicationPage({ limit, cursor }) {
      const params = {
        select: PUBLICATION_SELECT_FIELDS,
        order: 'round_revealed_at.desc,round_id.desc',
        limit: String(limit + 1),
      };
      if (cursor) {
        params.or = `(round_revealed_at.lt.${cursor.revealedAt},`
          + `and(round_revealed_at.eq.${cursor.revealedAt},round_id.lt.${cursor.roundId}))`;
      }
      return fetchRows('weekly_retrospective_publications', params);
    },
    async fetchAllPublications() {
      const pageSize = 1000;
      const maxRows = 10000;
      const rows = [];
      for (let offset = 0; offset < maxRows; offset += pageSize) {
        const page = await fetchRows(
          'weekly_retrospective_publications',
          {
            select: PUBLICATION_SELECT_FIELDS,
            order: 'round_revealed_at.asc,round_id.asc',
          },
          `${offset}-${offset + pageSize - 1}`,
        );
        rows.push(...page);
        if (page.length < pageSize) return rows.map(verifyPublicationCatalogRow);
      }
      throw new WeeklyRetrospectiveError('publication catalog limit exceeded');
    },
    fetchVerifiedContext,
    download,
  };
}

function optionalSingle(value, field) {
  if (value == null) return null;
  if (Array.isArray(value) || typeof value !== 'string' || !value) {
    throw new WeeklyRetrospectiveError(`${field} is invalid`);
  }
  return value;
}

function optionalFlag(value, field) {
  if (value == null) return false;
  if (Array.isArray(value) || value !== '1') {
    throw new WeeklyRetrospectiveError(`${field} is invalid`);
  }
  return true;
}

function normalizedOrigin(value) {
  if (typeof value !== 'string' || !value.trim()) return '';
  try {
    const url = new URL(value.trim());
    const loopbackHttp = url.protocol === 'http:'
      && ['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname);
    if ((url.protocol !== 'https:' && !loopbackHttp) || url.username || url.password) return '';
    return url.origin;
  } catch {
    return '';
  }
}

function normalizedServiceRoleKey(value) {
  if (typeof value !== 'string' || !value) return '';
  if (value.startsWith('sb_secret_') && value.length > 'sb_secret_'.length) return value;
  const parts = value.split('.');
  if (parts.length !== 3) return '';
  try {
    const payload = JSON.parse(Buffer.from(parts[1], 'base64url').toString('utf8'));
    return payload?.role === 'service_role' ? value : '';
  } catch {
    return '';
  }
}

function serviceHeaders(key) {
  const headers = { apikey: key };
  if (!key.startsWith('sb_secret_')) headers.Authorization = `Bearer ${key}`;
  return headers;
}

function etagMatches(headers = {}, etag) {
  const value = headers['if-none-match'] || headers['If-None-Match'];
  if (typeof value !== 'string') return false;
  return value.split(',').map(part => part.trim()).includes(etag)
    || value.trim() === '*';
}

function notModified(response) {
  response.status(304);
  if (typeof response.end === 'function') return response.end();
  return response.send?.();
}

function send(response, status, value) {
  response.setHeader('Content-Type', 'application/json; charset=utf-8');
  return response.status(status).json(value);
}

export default createWeeklyRetrospectivesHandler();
