import test from 'node:test';
import assert from 'node:assert/strict';
import { reconstructWeeklyViewerTrace } from '../weekly-trace-replay.js';

function fixture() {
  const entries = [
    { seq: 0, t_ms: 0, kind: 'state', snapshot: { data: { tree: 'initial' } } },
    { seq: 1, t_ms: 5, kind: 'app', action: 'question_start', state: { display_mode: 'grid' } },
    { seq: 2, t_ms: 20, kind: 'active_pane', pane_id: 'pose-B' },
    { seq: 3, t_ms: 40, kind: 'camera', camera: { radius: 12 }, source_pane_id: 'pose-B' },
    { seq: 4, t_ms: 60, kind: 'app', action: 'choice_rejected', state: { rejected_choice_ids: ['b'] } },
    { seq: 5, t_ms: 80, kind: 'app', action: 'choice_selected', state: { selected_choice_id: 'a' } },
    { seq: 6, t_ms: 100, kind: 'app', action: 'vote_submitted', state: { selected_choice_id: 'a' } },
  ];
  const appState = {
    display_mode: 'grid', selected_choice_id: 'a', rejected_choice_ids: ['b'],
    continuous_trace: { visit_id: 'visit-1', through_sequence: 6 },
  };
  const attempt = {
    vote_attempt_id: 'attempt-1', session_id: 'session-1', item_id: 'item-1',
    viewer_trace: null, app_state: appState,
  };
  const batches = [
    {
      trace_batch_id: 'batch-2', session_id: 'session-1', item_id: 'item-1', visit_id: 'visit-1',
      first_sequence: 4, last_sequence: 6, submitted_at: '2026-08-12T00:00:02Z',
      trace: { version: 1, visit_id: 'visit-1', entries: entries.slice(4) },
    },
    {
      trace_batch_id: 'batch-1', session_id: 'session-1', item_id: 'item-1', visit_id: 'visit-1',
      first_sequence: 0, last_sequence: 3, submitted_at: '2026-08-12T00:00:01Z',
      trace: { version: 1, visit_id: 'visit-1', entries: entries.slice(0, 4) },
    },
  ];
  return { attempt, batches, entries };
}

test('continuous batches reconstruct the same accepted entries and final state as the vote snapshot', () => {
  const { attempt, batches, entries } = fixture();
  const reconstructed = reconstructWeeklyViewerTrace(attempt, batches);
  const legacy = {
    version: 1, molstar_version: '4.6.0', duration_ms: 100, truncated: false,
    snapshots: entries.filter(entry => ['state', 'camera'].includes(entry.kind)),
    app_trace: entries.filter(entry => ['app', 'active_pane'].includes(entry.kind)),
    app_state: { display_mode: 'grid', selected_choice_id: 'a', rejected_choice_ids: ['b'] },
  };
  assert.deepEqual(reconstructed, legacy);
});

test('reconstructed continuous replay drives the same ordered player callbacks as legacy replay', async () => {
  const { attempt, batches, entries } = fixture();
  const reconstructed = reconstructWeeklyViewerTrace(attempt, batches);
  const legacy = {
    version: 1, molstar_version: '4.6.0', duration_ms: 100, truncated: false,
    snapshots: entries.filter(entry => ['state', 'camera'].includes(entry.kind)),
    app_trace: entries.filter(entry => ['app', 'active_pane'].includes(entry.kind)),
    app_state: { display_mode: 'grid', selected_choice_id: 'a', rejected_choice_ids: ['b'] },
  };
  const callbackTimeline = trace => [
    ...trace.snapshots.map(entry => `${entry.seq}:${entry.kind}`),
    ...(trace.app_trace || []).map(entry => `${entry.seq}:${entry.kind}:${entry.action || entry.pane_id}`),
  ].sort((left, right) => Number(left.split(':')[0]) - Number(right.split(':')[0]));
  assert.deepEqual(callbackTimeline(reconstructed), callbackTimeline(legacy));
});

test('a retry can append later events without changing the earlier vote boundary', () => {
  const { attempt, batches } = fixture();
  batches.push({
    trace_batch_id: 'later', session_id: 'session-1', item_id: 'item-1', visit_id: 'visit-1',
    first_sequence: 7, last_sequence: 8, submitted_at: '2026-08-12T00:00:03Z',
    trace: { version: 1, visit_id: 'visit-1', entries: [
      { seq: 7, t_ms: 110, kind: 'app', action: 'vote_retry' },
      { seq: 8, t_ms: 120, kind: 'camera', camera: { radius: 5 } },
    ] },
  });
  const reconstructed = reconstructWeeklyViewerTrace(attempt, batches);
  assert.equal(reconstructed.app_trace.at(-1).action, 'vote_submitted');
  assert.equal(reconstructed.snapshots.at(-1).camera.radius, 12);
});

test('old vote snapshots remain the authoritative backward-compatible replay', () => {
  const legacy = { version: 1, molstar_version: '4.6.0', snapshots: [] };
  assert.equal(reconstructWeeklyViewerTrace({ viewer_trace: legacy }, []), legacy);
});

test('a missing continuous sequence is explicit rather than silently presented as complete', () => {
  const { attempt, batches } = fixture();
  batches[0].trace.entries = batches[0].trace.entries.filter(entry => entry.seq !== 5);
  const reconstructed = reconstructWeeklyViewerTrace(attempt, batches);
  assert.equal(reconstructed.truncated, true);
  assert.equal(reconstructed.continuous_trace_incomplete, true);
});

test('an explicit omitted entry preserves ordering while flagging replay as truncated', () => {
  const { attempt, batches } = fixture();
  batches[0].trace.entries[1] = {
    seq: 5,
    t_ms: 80,
    kind: 'omitted',
    omitted_kind: 'state',
    omitted_entry_count: 1,
    omitted_bytes: 600000,
    reason: 'single_entry_byte_budget',
  };
  const reconstructed = reconstructWeeklyViewerTrace(attempt, batches);
  assert.equal(reconstructed.truncated, true);
  assert.equal(reconstructed.byte_compacted, true);
  assert.equal(reconstructed.continuous_trace_incomplete, true);
  assert.equal(reconstructed.app_trace.at(-1).action, 'vote_submitted');
});

test('semantic-only continuous traces remain replayable as an incomplete app timeline', () => {
  const attempt = {
    session_id: 'session-semantic', item_id: 'item-semantic', viewer_trace: null,
    app_state: { continuous_trace: { visit_id: 'visit-semantic', through_sequence: 1 } },
  };
  const batches = [{
    session_id: 'session-semantic', item_id: 'item-semantic', visit_id: 'visit-semantic',
    first_sequence: 0, last_sequence: 1,
    trace: { version: 1, molstar_version: '4.6.0', visit_id: 'visit-semantic', entries: [
      { seq: 0, t_ms: 0, kind: 'omitted', omitted_kind: 'state', reason: 'single_entry_byte_budget' },
      { seq: 1, t_ms: 1, kind: 'app', action: 'vote_submitted' },
    ] },
  }];
  const trace = reconstructWeeklyViewerTrace(attempt, batches);
  assert.deepEqual(trace.snapshots, []);
  assert.equal(trace.app_trace[0].action, 'vote_submitted');
  assert.equal(trace.continuous_trace_incomplete, true);
});
