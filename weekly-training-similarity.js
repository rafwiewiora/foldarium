export const WEEKLY_SIMILARITY_FORMATS = Object.freeze([
  'foldarium.weekly-training-similarity-report/v1',
  'foldarium.weekly-training-similarity-report/v2',
]);
export const WEEKLY_SIMILARITY_TRAINING_CUTOFF = '2021-09-30';
export const WEEKLY_SIMILARITY_NOVELTY_THRESHOLD = 0.25;
export const WEEKLY_SIMILARITY_SORT_MODES = Object.freeze([
  'default',
  'novel-first',
  'familiar-first',
]);

const CLASSIFICATIONS = new Set(['familiar', 'novel', 'unknown']);
const SCORE_FIELDS = ['train_identity', 'train_shape_overlap', 'nearest_score', 'pocket_aware_score'];
const OVERLAY_FIELDS = ['overlay', 'training_overlay', 'training_system_overlay'];
const SHA256 = /^[0-9a-f]{64}$/;
const PDB_ID = /^[0-9][A-Za-z0-9]{3}$/;
const ITEM_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$/;
const COMPONENT_ID = /^[A-Za-z0-9]{1,8}$/;
const WEEK = /^\d{4}-\d{2}-\d{2}$/;

export class WeeklySimilarityContractError extends Error {
  constructor(message) {
    super(message);
    this.name = 'WeeklySimilarityContractError';
  }
}

function isObject(value) {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function optionalText(value, label, pattern = null) {
  if (value === null || value === undefined) return null;
  if (typeof value !== 'string' || !value || (pattern && !pattern.test(value))) {
    throw new WeeklySimilarityContractError(`${label} is invalid`);
  }
  return value;
}

function boundedScore(value, label) {
  if (value === null || value === undefined) return null;
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0 || value > 1) {
    throw new WeeklySimilarityContractError(`${label} must be between 0 and 1`);
  }
  return value;
}

function optionalNonnegativeNumber(value, label) {
  if (value === null || value === undefined) return null;
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) {
    throw new WeeklySimilarityContractError(`${label} must be a nonnegative number`);
  }
  return value;
}

function optionalClassification(value, label) {
  if (value === null || value === undefined) return null;
  if (!CLASSIFICATIONS.has(value)) {
    throw new WeeklySimilarityContractError(`${label} is invalid`);
  }
  return value;
}

export function parseContentAddressedSupabaseOverlay(value) {
  if (value === null || value === undefined) return null;
  if (!isObject(value)) {
    throw new WeeklySimilarityContractError('overlay descriptor must be an object');
  }
  const objectUri = value.object_uri;
  const sha256 = value.sha256;
  const sizeBytes = value.size_bytes;
  const mediaType = value.media_type;
  if (typeof objectUri !== 'string' || typeof sha256 !== 'string' || !SHA256.test(sha256)) {
    throw new WeeklySimilarityContractError('overlay digest is invalid');
  }
  const match = /^supabase:\/\/([^/?#]+)\/sha256\/([0-9a-f]{2})\/([0-9a-f]{64})$/.exec(
    objectUri,
  );
  if (!match || match[2] !== sha256.slice(0, 2) || match[3] !== sha256) {
    throw new WeeklySimilarityContractError('overlay URI is not content-addressed by its digest');
  }
  if (!Number.isSafeInteger(sizeBytes) || sizeBytes <= 0) {
    throw new WeeklySimilarityContractError('overlay size_bytes is invalid');
  }
  if (mediaType !== 'chemical/x-pdb') {
    throw new WeeklySimilarityContractError('overlay media_type is invalid');
  }
  return Object.freeze({
    object_uri: objectUri,
    sha256,
    size_bytes: sizeBytes,
    media_type: mediaType,
  });
}

function parseRecord(raw, index) {
  if (!isObject(raw)) {
    throw new WeeklySimilarityContractError(`record ${index} must be an object`);
  }
  const week = optionalText(raw.week, `record ${index} week`, WEEK);
  const itemId = optionalText(raw.item_id, `record ${index} item_id`, ITEM_ID);
  if (!week || !itemId) {
    throw new WeeklySimilarityContractError(`record ${index} identity is missing`);
  }
  const classification = raw.classification;
  if (!CLASSIFICATIONS.has(classification)) {
    throw new WeeklySimilarityContractError(`record ${index} classification is invalid`);
  }
  const scores = Object.fromEntries(SCORE_FIELDS.map(field => [
    field,
    boundedScore(raw[field], `record ${index} ${field}`),
  ]));
  const similarity = scores.train_shape_overlap;
  if (classification === 'familiar'
    && (similarity === null || similarity < WEEKLY_SIMILARITY_NOVELTY_THRESHOLD)) {
    throw new WeeklySimilarityContractError(`record ${index} familiar classification is inconsistent`);
  }
  if (classification === 'novel'
    && similarity !== null && similarity >= WEEKLY_SIMILARITY_NOVELTY_THRESHOLD) {
    throw new WeeklySimilarityContractError(`record ${index} novel classification is inconsistent`);
  }
  if (classification === 'unknown' && similarity !== null) {
    throw new WeeklySimilarityContractError(`record ${index} unknown classification has a score`);
  }
  const trainPdb = optionalText(raw.train_pdb, `record ${index} train_pdb`, PDB_ID);
  const trainHet = optionalText(raw.train_het, `record ${index} train_het`, COMPONENT_ID);
  if ((similarity === null) !== (trainPdb === null && trainHet === null)
    || (trainPdb === null) !== (trainHet === null)) {
    throw new WeeklySimilarityContractError(`record ${index} score and source are inconsistent`);
  }
  if (raw.has_correct_pose !== null
    && raw.has_correct_pose !== undefined
    && typeof raw.has_correct_pose !== 'boolean') {
    throw new WeeklySimilarityContractError(`record ${index} has_correct_pose is invalid`);
  }
  const overlayFields = OVERLAY_FIELDS.filter(field => raw[field] !== null && raw[field] !== undefined);
  if (overlayFields.length > 1) {
    throw new WeeklySimilarityContractError(`record ${index} has multiple overlay descriptors`);
  }
  const overlay = parseContentAddressedSupabaseOverlay(
    overlayFields.length ? raw[overlayFields[0]] : null,
  );
  if (overlay && similarity === null) {
    throw new WeeklySimilarityContractError(`record ${index} overlay has no scored training source`);
  }
  return Object.freeze({
    ...raw,
    week,
    item_id: itemId,
    ligand: optionalText(raw.ligand, `record ${index} ligand`, COMPONENT_ID),
    classification,
    train_pdb: trainPdb,
    train_het: trainHet,
    ...scores,
    nearest_classification: optionalClassification(
      raw.nearest_classification,
      `record ${index} nearest_classification`,
    ),
    pocket_aware_classification: optionalClassification(
      raw.pocket_aware_classification,
      `record ${index} pocket_aware_classification`,
    ),
    train_align_rmsd: optionalNonnegativeNumber(
      raw.train_align_rmsd,
      `record ${index} train_align_rmsd`,
    ),
    overlay,
  });
}

export function similarityRecordKey(week, itemId) {
  return `${week}\u0000${itemId}`;
}

export function parseWeeklyTrainingSimilarityReport(value) {
  if (!isObject(value)) {
    throw new WeeklySimilarityContractError('similarity report must be an object');
  }
  if (!WEEKLY_SIMILARITY_FORMATS.includes(value.format_version)) {
    throw new WeeklySimilarityContractError('similarity report format_version is unsupported');
  }
  if (value.training_cutoff !== WEEKLY_SIMILARITY_TRAINING_CUTOFF) {
    throw new WeeklySimilarityContractError('similarity report training_cutoff is unsupported');
  }
  if (value.novelty_threshold !== WEEKLY_SIMILARITY_NOVELTY_THRESHOLD) {
    throw new WeeklySimilarityContractError('similarity report novelty_threshold is unsupported');
  }
  if (!Array.isArray(value.records)) {
    throw new WeeklySimilarityContractError('similarity report records are missing');
  }
  const records = value.records.map(parseRecord);
  const recordsByKey = new Map();
  for (const record of records) {
    const key = similarityRecordKey(record.week, record.item_id);
    if (recordsByKey.has(key)) {
      throw new WeeklySimilarityContractError('similarity report has duplicate item/week records');
    }
    recordsByKey.set(key, record);
  }
  return Object.freeze({
    format_version: value.format_version,
    training_cutoff: value.training_cutoff,
    novelty_threshold: value.novelty_threshold,
    records: Object.freeze(records),
    recordsByKey,
  });
}

export function weeklySimilarityRecord(report, week, itemId) {
  return report?.recordsByKey?.get(similarityRecordKey(week, itemId)) || null;
}

function publicationIndex(row) {
  return Number.isSafeInteger(row?.publicationIndex)
    ? row.publicationIndex
    : Number.isSafeInteger(row?.index) ? row.index : 0;
}

function sortableSimilarity(row) {
  const similarity = row?.similarity;
  if (!similarity || similarity.classification === 'unknown') {
    return { kind: 'unknown', score: null };
  }
  if (similarity.classification === 'novel' && similarity.train_shape_overlap === null) {
    return { kind: 'no-analog', score: null };
  }
  return { kind: 'numeric', score: similarity.train_shape_overlap };
}

export function compareWeeklySimilarityRows(left, right, mode = 'default') {
  if (!WEEKLY_SIMILARITY_SORT_MODES.includes(mode)) {
    throw new WeeklySimilarityContractError(`unknown similarity sort mode: ${mode}`);
  }
  const originalOrder = publicationIndex(left) - publicationIndex(right);
  if (mode === 'default') return originalOrder;
  const leftValue = sortableSimilarity(left);
  const rightValue = sortableSimilarity(right);
  const order = mode === 'novel-first'
    ? { 'no-analog': 0, numeric: 1, unknown: 2 }
    : { numeric: 0, 'no-analog': 1, unknown: 2 };
  const kindOrder = order[leftValue.kind] - order[rightValue.kind];
  if (kindOrder) return kindOrder;
  if (leftValue.kind === 'numeric') {
    const scoreOrder = mode === 'novel-first'
      ? leftValue.score - rightValue.score
      : rightValue.score - leftValue.score;
    if (scoreOrder) return scoreOrder;
  }
  return originalOrder;
}

export function sortWeeklySimilarityRows(rows, mode = 'default') {
  return [...rows].sort((left, right) => compareWeeklySimilarityRows(left, right, mode));
}

export async function fetchWeeklyTrainingSimilarityReport(
  url = '/docs/weekly-training-similarity-results.json',
  fetchImpl = globalThis.fetch,
) {
  const response = await fetchImpl(url);
  if (!response?.ok) throw new Error('Training similarity report is unavailable');
  return parseWeeklyTrainingSimilarityReport(await response.json());
}
