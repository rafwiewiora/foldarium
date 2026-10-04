const SHA256 = /^[0-9a-f]{64}$/;

export function parseWeeklyQuestionScope(value) {
  if (value?.mode === 'all') return { mode: 'all' };
  if (value?.mode !== 'featured' || !SHA256.test(value.blind_manifest_sha256)
      || !Array.isArray(value.item_ids) || !value.item_ids.length || value.item_ids.length > 5
      || value.item_ids.some(id => typeof id !== 'string' || !id || id.length > 200)
      || new Set(value.item_ids).size !== value.item_ids.length) return null;
  return {
    mode: 'featured',
    blind_manifest_sha256: value.blind_manifest_sha256,
    item_ids: [...value.item_ids],
  };
}

export function featuredQuestionScope(round) {
  const marker = round?.featured_questions;
  const items = round?.blind_manifest?.items;
  if (!marker || !Array.isArray(items) || marker.schema_version !== 1
      || marker.policy !== 'foldarium-weekly-question-draw/v1'
      || !['uniform', 'interestingness_weighted'].includes(marker.mode)
      || typeof marker.seed !== 'string' || !marker.seed
      || !SHA256.test(marker.candidate_population_sha256)
      || !SHA256.test(marker.selection_sha256)
      || marker.blind_manifest_sha256 !== round.blind_manifest_sha256
      || marker.source_item_count !== items.length
      || !Number.isSafeInteger(marker.candidate_count)
      || marker.candidate_count > items.length || marker.candidate_count < 1
      || !Number.isSafeInteger(marker.requested_question_count)
      || marker.requested_question_count < 1 || marker.requested_question_count > 5
      || marker.selected_question_count !== Math.min(marker.requested_question_count, marker.candidate_count)
      || marker.selected_question_count !== marker.item_ids?.length) return null;
  const scope = parseWeeklyQuestionScope({
    mode: 'featured',
    blind_manifest_sha256: marker.blind_manifest_sha256,
    item_ids: marker.item_ids,
  });
  if (!scope) return null;
  const ids = new Set(items.map(item => item.id));
  return ids.size === items.length && scope.item_ids.every(id => ids.has(id)) ? scope : null;
}

// Preserve the full manifest's ordinal: vote and trace APIs bind it to item ID.
export function scopedWeeklyQuestionIndexes(items, scope) {
  const all = items.map((_, index) => index);
  if (scope?.mode !== 'featured') return all;
  const selected = new Set(scope.item_ids);
  const indexes = all.filter(index => selected.has(items[index].id));
  if (indexes.length !== selected.size) throw new Error('Featured questions are unavailable in this round.');
  return indexes;
}

// Result scope is separate from navigation scope and never authorizes a vote.
export function validateFeaturedResults(value, round) {
  if (value == null) return null;
  const items = round?.blind_manifest?.items;
  if (value.format_version !== 'foldarium.weekly-featured-results/v1'
    || value.scope !== 'featured' || value.round_id !== round?.round_id
    || !Array.isArray(items) || value.full_round_item_count !== items.length
    || !Array.isArray(value.item_ids) || value.assignment_total !== value.item_ids.length
    || !Number.isSafeInteger(value.assignment_total) || value.assignment_total < 1 || value.assignment_total > 5
    || new Set(value.item_ids).size !== value.assignment_total
    || value.item_ids.some(id => !items.some(item => item.id === id))
    || !Number.isSafeInteger(value.scorable_item_count) || value.scorable_item_count < 0
    || value.scorable_item_count > value.assignment_total || !Array.isArray(value.participants)) {
    throw new Error('Featured results are invalid.');
  }
  for (const row of value.participants) {
    if (!row || typeof row.participant !== 'string' || !['human', 'llm', 'baseline'].includes(row.participant_kind)
      || row.assignment_total !== value.assignment_total || row.total !== value.scorable_item_count
      || !Number.isSafeInteger(row.assignment_answered) || row.assignment_answered < 1 || row.assignment_answered > row.assignment_total
      || row.assignment_complete !== (row.assignment_answered === row.assignment_total)
      || !Number.isSafeInteger(row.answered) || row.answered < 0 || row.answered > row.total
      || !Number.isSafeInteger(row.correct) || row.correct < 0 || row.correct > row.answered
      || row.excluded_answered !== row.assignment_answered - row.answered
      || row.excluded_item_count !== row.assignment_total - row.total
      || row.excluded_answered < 0 || row.excluded_answered > row.excluded_item_count
      || row.accuracy !== (row.answered ? Math.round(10000 * row.correct / row.answered) / 100 : null)) {
      throw new Error('Featured participant result is invalid.');
    }
  }
  return value;
}

export function featuredResultText(row) {
  const completion = `${row.assignment_answered}/${row.assignment_total} featured ${row.assignment_complete ? 'complete' : 'answered'}`;
  const score = row.answered ? `${row.correct}/${row.answered} scored correct` : 'No scorable answers';
  return `${completion} · ${score}${row.excluded_answered ? ` · ${row.excluded_answered} not scored` : ''}`;
}
