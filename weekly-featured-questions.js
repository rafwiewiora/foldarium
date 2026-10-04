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
