import { createHash } from 'node:crypto';
import { featuredQuestionScope } from '../weekly-featured-questions.js';
import { isUnscorableReference } from './reference-disposition.js';
import { WeeklyRetrospectiveError, assertResponseSafe, parseSupabaseObjectUri,
  withLigandPlddtBaseline } from './weekly-retrospectives.js';

export const FEATURED_RESULTS_VERSION = 'foldarium.weekly-featured-results/v1';
export const FEATURED_ALL_TIME_VERSION = 'foldarium.weekly-featured-all-time/v1';
const bindings = new WeakMap();
const fail = message => { throw new WeeklyRetrospectiveError(message); };
const sha = bytes => createHash('sha256').update(bytes).digest('hex');
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const percent = (a, b) => b ? Math.round(10000 * a / b) / 100 : null;
const binding = p => JSON.stringify([p.roundId, p.digests.blind_manifest_sha256, p.closesAt]);

// This object is fetched through a service-only RPC, never from a browser ballot.
export function featuredSelectionDescriptor(row) {
  const artifact = row?.selection_artifact;
  if (!artifact || artifact.media_type !== 'application/json'
    || !Number.isSafeInteger(artifact.size_bytes) || artifact.size_bytes < 1 || artifact.size_bytes > 10485760) {
    fail('featured selection artifact descriptor is invalid');
  }
  return { ...parseSupabaseObjectUri(artifact.object_uri, artifact.sha256),
    sha256: artifact.sha256, sizeBytes: artifact.size_bytes, mediaType: artifact.media_type };
}

export function verifyFeaturedSelection(row, bytes, publication, context) {
  if (row === null) return null;
  if (!row || row.round_id !== publication.roundId || row.environment !== 'production'
    || row.blind_manifest_sha256 !== publication.digests.blind_manifest_sha256
    || !Number.isFinite(Date.parse(row.registered_at))) fail('featured selection catalog binding is invalid');
  const descriptor = featuredSelectionDescriptor(row);
  if (!Buffer.isBuffer(bytes) || bytes.length !== descriptor.sizeBytes || sha(bytes) !== descriptor.sha256) {
    fail('featured selection artifact digest is invalid');
  }
  let audit;
  try { audit = JSON.parse(bytes.toString('utf8')); } catch { fail('featured selection artifact is invalid JSON'); }
  const marker = row.featured_questions;
  const scope = featuredQuestionScope({ featured_questions: marker, blind_manifest: context.blindManifest,
    blind_manifest_sha256: publication.digests.blind_manifest_sha256 });
  if (!scope || marker.selection_sha256 !== descriptor.sha256 || audit.schema_version !== 1
    || audit.source_round_id !== publication.roundId
    || audit.source_blind_manifest_sha256 !== publication.digests.blind_manifest_sha256
    || audit.source_private_index_sha256 !== publication.digests.private_index_sha256
    || !same(audit.included_item_ids, scope.item_ids)
    || ['policy', 'mode', 'seed', 'candidate_population_sha256', 'source_item_count', 'candidate_count',
      'requested_question_count', 'selected_question_count'].some(key => audit[key] !== marker[key])) {
    fail('featured selection audit differs from the frozen source');
  }
  // A post-close draw was not an assignment voters could complete. Do not rewrite history.
  if (Date.parse(row.registered_at) >= Date.parse(publication.closesAt)) return null;
  const selection = Object.freeze({ itemIds: Object.freeze([...scope.item_ids]), mode: marker.mode });
  bindings.set(selection, binding(publication));
  return selection;
}

function scoreRows({ publication, context, sourceSnapshot, publicArtifact, selection }) {
  if (bindings.get(selection) !== binding(publication)) fail('featured cohort is not bound to this publication');
  const selected = new Set(selection.itemIds);
  const answers = new Map(context.revealManifest.items.map(item => [item.id, item]));
  if (answers.size !== publication.itemCount || answers.size !== context.revealManifest.items.length
    || context.blindManifest.items.length !== answers.size
    || context.blindManifest.items.some(item => !answers.has(item.id))) fail('featured answer population is invalid');
  for (const item of answers.values()) {
    const unscorable = isUnscorableReference(item);
    if (!Array.isArray(item.choices) || !item.choices.length
      || new Set(item.choices.map(choice => choice.id)).size !== item.choices.length
      || (!unscorable && item.choices.some(choice => typeof choice.correct !== 'boolean'
        || typeof choice.accepted_correct !== 'boolean' || !Number.isFinite(choice.rmsd) || choice.rmsd < 0))) {
      fail('featured answer correctness is invalid');
    }
  }
  const excluded = selection.itemIds.filter(id => isUnscorableReference(answers.get(id))).length;
  const total = selected.size - excluded;
  const rows = new Map();
  for (const participant of sourceSnapshot.participants) {
    rows.set(participant.participantLink, { key: `${participant.participantKind}:${participant.participantKind === 'human'
      ? participant.participantLink : participant.displayName}`, participant: participant.displayName,
    participant_kind: participant.participantKind, correct: 0, answered: 0, assignment_answered: 0,
    full_round_answered: 0, seen: new Set() });
  }
  for (const vote of sourceSnapshot.votes) {
    const row = rows.get(vote.participantLink);
    const item = answers.get(vote.itemId);
    const choice = item?.choices.find(candidate => candidate.id === vote.choiceId);
    if (!row || !item || row.seen.has(vote.itemId) || typeof vote.pickedNone !== 'boolean'
      || (vote.pickedNone
        ? vote.selectionKind !== 'none' || vote.choiceId !== null
        : !['exact', 'cluster'].includes(vote.selectionKind) || !choice)) fail('featured source vote is invalid');
    row.seen.add(vote.itemId);
    row.full_round_answered += 1;
    if (!selected.has(vote.itemId)) continue;
    row.assignment_answered += 1;
    if (isUnscorableReference(item)) continue;
    row.answered += 1;
    // Exact ballots and baselines score the selected raw pose. A cluster ballot
    // accepts any correct member; None is correct only when no raw pose passes.
    const correct = vote.pickedNone ? !item.choices.some(c => c.correct)
      : vote.selectionKind === 'cluster' ? choice.accepted_correct : choice.correct;
    if (correct === true) row.correct += 1;
  }
  const scored = withLigandPlddtBaseline(publicArtifact, context);
  for (const participant of scored.automated_entries.filter(row => row.participant_kind === 'baseline')) {
    const row = { key: `baseline:${participant.participant}`, participant: participant.participant,
      participant_kind: 'baseline', correct: 0, answered: 0, assignment_answered: 0,
      full_round_answered: scored.questions.length };
    for (const question of scored.questions.filter(q => selected.has(q.item_id))) {
      const response = question.automated_entries.find(r => r.participant === row.participant && r.participant_kind === 'baseline');
      if (!response) fail('featured baseline response is missing');
      row.assignment_answered += 1;
      if (isUnscorableReference(answers.get(question.item_id))) continue;
      if (typeof response.correct !== 'boolean') fail('featured baseline correctness is invalid');
      row.answered += 1;
      if (response.correct) row.correct += 1;
    }
    rows.set(row.key, row);
  }
  return [...rows.values()].filter(row => row.assignment_answered > 0).map(({ seen, ...row }) => ({
    ...row, total, accuracy: percent(row.correct, row.answered),
    assignment_total: selected.size, assignment_complete: row.assignment_answered === selected.size,
    excluded_answered: row.assignment_answered - row.answered,
    excluded_item_count: excluded, full_round_total: publication.itemCount,
  }));
}

export function buildFeaturedResults(week) {
  if (!week.selection) return null;
  const rows = scoreRows(week).map(({ key, ...row }) => row).sort((a, b) =>
    Number(b.assignment_complete) - Number(a.assignment_complete) || b.correct - a.correct
    || b.answered - a.answered || a.participant.localeCompare(b.participant));
  const result = { format_version: FEATURED_RESULTS_VERSION, round_id: week.publication.roundId,
    scope: 'featured', selection_mode: week.selection.mode, item_ids: [...week.selection.itemIds],
    assignment_total: week.selection.itemIds.length, scorable_item_count: rows[0]?.total
      ?? week.selection.itemIds.filter(id => !isUnscorableReference(week.context.revealManifest.items.find(i => i.id === id))).length,
    full_round_item_count: week.publication.itemCount,
    participants: rows };
  assertResponseSafe(result);
  return result;
}

export function buildFeaturedAllTime(weeks, { ranking = 'total_correct', participantKind = null } = {}) {
  if (!['total_correct', 'weighted_average_accuracy'].includes(ranking)
    || (participantKind !== null && !['human', 'llm', 'baseline'].includes(participantKind))) fail('featured ranking is invalid');
  const aggregates = new Map();
  const eligible = weeks.filter(week => week.selection);
  for (const week of eligible) {
    for (const row of scoreRows(week)) {
      const aggregate = aggregates.get(row.key) || { participant: row.participant, participant_kind: row.participant_kind,
        weeks_participated: 0, complete_weeks: 0, total_correct: 0, total_questions: 0,
        assignment_answers: 0, assignment_questions: 0, excluded_questions: 0, latest_at: '' };
      aggregate.weeks_participated += 1;
      aggregate.assignment_answers += row.assignment_answered;
      aggregate.assignment_questions += row.assignment_total;
      if (row.assignment_complete) {
        aggregate.complete_weeks += 1;
        aggregate.total_correct += row.correct;
        aggregate.total_questions += row.total;
        aggregate.excluded_questions += row.excluded_item_count;
      }
      if (Date.parse(week.publication.revealedAt) >= Date.parse(aggregate.latest_at || '1970-01-01')) {
        aggregate.participant = row.participant;
        aggregate.latest_at = week.publication.revealedAt;
      }
      aggregates.set(row.key, aggregate);
    }
  }
  const rows = [...aggregates.values()].filter(row => !participantKind || row.participant_kind === participantKind)
    .map(({ latest_at, ...row }) => ({ ...row,
      weighted_average_accuracy: percent(row.total_correct, row.total_questions), provisional: row.complete_weeks < 3 }))
    .sort((a, b) => Number(b.total_questions > 0) - Number(a.total_questions > 0)
      || (ranking === 'weighted_average_accuracy'
        ? (b.weighted_average_accuracy ?? -1) - (a.weighted_average_accuracy ?? -1) : b.total_correct - a.total_correct)
      || b.total_questions - a.total_questions || a.participant.localeCompare(b.participant));
  const result = { format_version: FEATURED_ALL_TIME_VERSION, scope: 'featured', eligible_week_count: eligible.length,
    ranking, participant_kind: participantKind, participants: rows.map((row, index) => ({ ...row,
      rank: row.total_questions > 0 ? index + 1 : null })) };
  assertResponseSafe(result);
  return result;
}
