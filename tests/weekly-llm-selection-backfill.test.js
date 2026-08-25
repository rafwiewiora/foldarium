import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';

const repositoryRoot = path.resolve(new URL('..', import.meta.url).pathname);
const toolPath = path.join(repositoryRoot, 'local/backfill_weekly_llm_selection_scope.py');
const SOURCE_ROUND = 'weekly-2026-08-08-beta-v4';
const TARGET_ROUND = 'weekly-2026-08-08-beta-v5-global-tm-29';
const LABELS = ['Claude Opus', 'Codex GPT-5.6'];

function canonical(value) {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
  if (value && typeof value === 'object') {
    return `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${canonical(value[key])}`).join(',')}}`;
  }
  return JSON.stringify(value);
}

function digest(value) {
  return createHash('sha256').update(canonical(value)).digest('hex');
}

function id(number) {
  return `00000000-0000-4000-8000-${String(number).padStart(12, '0')}`;
}

function fixture(itemCount = 21, sourceItemCount = 24) {
  const sourceItems = [];
  const targetItems = [];
  for (let index = 0; index < sourceItemCount; index += 1) {
    const itemId = `item-${String(index).padStart(2, '0')}`;
    const choices = [
      { id: `rep-${index}`, cluster_id: `cluster-${index}`, is_rep: true },
      { id: `member-${index}`, cluster_id: `cluster-${index}`, is_rep: false },
    ];
    sourceItems.push({ id: itemId, choices: structuredClone(choices) });
    if (index < itemCount) {
      targetItems.push({ id: itemId, choices: structuredClone(choices) });
    }
  }
  const sourceManifest = { schema_version: 1, round_id: SOURCE_ROUND, items: sourceItems };
  const targetManifest = { schema_version: 1, round_id: TARGET_ROUND, items: targetItems };
  const rounds = [
    {
      round_id: SOURCE_ROUND,
      item_count: sourceItemCount,
      blind_manifest: sourceManifest,
      blind_manifest_sha256: digest(sourceManifest),
    },
    {
      round_id: TARGET_ROUND,
      item_count: itemCount,
      blind_manifest: targetManifest,
      blind_manifest_sha256: digest(targetManifest),
    },
  ];

  const sessions = [];
  const votes = [];
  const attempts = [];
  LABELS.forEach((label, modelIndex) => {
    const userId = id(100 + modelIndex);
    const sourceSessionId = id(200 + modelIndex);
    const targetSessionId = id(300 + modelIndex);
    sessions.push({
      session_id: sourceSessionId,
      round_id: SOURCE_ROUND,
      user_id: userId,
      display_name: label,
      initial_app_state: {
        participant_type: 'llm',
        model_label: label,
        round_id: SOURCE_ROUND,
      },
      completed_at: '2026-08-09T00:00:00+00:00',
    });
    sessions.push({
      session_id: targetSessionId,
      round_id: TARGET_ROUND,
      user_id: userId,
      display_name: label,
      initial_app_state: {
        participant_type: 'llm',
        model_label: label,
        round_id: SOURCE_ROUND,
      },
      completed_at: '2026-08-09T00:00:00+00:00',
    });
    targetItems.forEach((item, itemIndex) => {
      const pickedNone = itemIndex % 5 === 0;
      const choiceId = pickedNone ? null : item.choices[0].id;
      const timestamp = new Date(Date.UTC(2026, 7, 9, modelIndex, itemIndex, 0))
        .toISOString().replace('Z', '+00:00');
      const attemptId = id(1000 + modelIndex * itemCount + itemIndex);
      votes.push({
        vote_id: attemptId,
        round_id: TARGET_ROUND,
        user_id: userId,
        item_id: item.id,
        choice_id: choiceId,
        picked_none: pickedNone,
        submitted_at: timestamp,
        selection_kind: null,
        selection_id: null,
        selection_source_attempt_id: null,
        selection_revision: 0,
        selection_source: null,
        selection_resolution_id: null,
      });
      attempts.push({
        vote_attempt_id: attemptId,
        session_id: targetSessionId,
        round_id: TARGET_ROUND,
        user_id: userId,
        item_id: item.id,
        question_index: itemIndex,
        choice_id: choiceId,
        picked_none: pickedNone,
        selection_kind: null,
        selection_id: null,
        viewer_trace: null,
        app_state: {
          schema_version: 1,
          participant_type: 'llm',
          model_label: label,
          item_id: item.id,
          question_index: itemIndex,
          choice_id: choiceId,
          picked_none: pickedNone,
          confidence_0_to_1: 0.75,
        },
        active_pane_id: null,
        vote_comment: null,
        submitted_at: timestamp,
        created_at: timestamp,
      });
    });
  });
  return { rounds, sessions, votes, attempts };
}

async function evidenceRoot() {
  const root = await mkdtemp(path.join(tmpdir(), 'foldarium-llm-evidence-'));
  await mkdir(path.join(root, 'local'));
  await Promise.all([
    writeFile(path.join(root, 'local/build_llm_vote_packets.py'), 'print("packet builder")\n'),
    writeFile(path.join(root, 'local/validate_llm_ballots.py'), 'print("ballot validator")\n'),
    writeFile(path.join(root, 'local/submit_llm_ballots.py'), 'print("ballot submitter")\n'),
  ]);
  return root;
}

const harness = String.raw`
import copy
import importlib.util
import json
import pathlib
import sys

sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location("backfill", sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
request = json.load(sys.stdin)
snapshot = request["snapshot"]
root = pathlib.Path(request["repo_root"])

def evidence():
    source, target = module._index_rounds(snapshot)
    _sm, source_digest = module._validate_round(source, module.SOURCE_ROUND_ID)
    _tm, target_digest = module._validate_round(target, module.TARGET_ROUND_ID)
    return module.build_evidence_bundle(
        root,
        source_manifest_sha256=source_digest,
        target_manifest_sha256=target_digest,
    )

try:
    bundle = evidence()
    if request["action"] == "evidence":
        result = {"sha256": bundle.sha256, "manifest": bundle.manifest}
    else:
        plan = module.prepare_resolution_plan(
            snapshot,
            repo_root=root,
            expected_evidence_sha256=request.get("expected_evidence_sha256", bundle.sha256),
            actor="reviewed-operator",
            reviewer="independent-reviewer",
            reason="Reviewed historical cluster-card ballot procedure.",
            expected_vote_count=request.get("expected_vote_count"),
        )
        if request["action"] == "plan":
            cluster = next(row for row in plan.entries if row["selection_kind"] == "cluster")
            none = next(row for row in plan.entries if row["selection_kind"] == "none")
            result = {
                "report": plan.report,
                "cluster": cluster["payload"],
                "none": none["payload"],
            }
        elif request["action"] == "execute":
            class FakeClient:
                def __init__(self):
                    self.calls = []
                def rpc(self, name, payload):
                    self.calls.append((name, copy.deepcopy(payload)))
                    if name == "resolve_weekly_quiz_vote_selection":
                        return {"resolution_id": payload["p_resolution_id"]}
                    return [{
                        "round_id": module.TARGET_ROUND_ID,
                        "total_votes": plan.report["expected_vote_count"],
                        "resolved_votes": plan.report["expected_vote_count"],
                        "unresolved_votes": 0,
                        "inconsistent_votes": 0,
                        "ready": True,
                    }]
            dry_client = FakeClient()
            dry_report = module.execute_resolution_plan(plan, client=dry_client, apply=False)
            apply_client = FakeClient()
            apply_report = module.execute_resolution_plan(plan, client=apply_client, apply=True)
            result = {
                "dry_calls": len(dry_client.calls),
                "dry_report": dry_report,
                "apply_calls": len(apply_client.calls),
                "rpc_names": [name for name, _payload in apply_client.calls],
                "first_payload": apply_client.calls[0][1],
                "apply_report": apply_report,
            }
        else:
            raise AssertionError("unknown action")
except module.BackfillError as error:
    result = {"error": str(error)}

print(json.dumps(result, sort_keys=True))
`;

function runHarness(action, snapshot, root, options = {}) {
  const result = spawnSync('python3', ['-c', harness, toolPath], {
    input: JSON.stringify({
      action,
      snapshot,
      repo_root: root,
      ...options,
    }),
    encoding: 'utf8',
  });
  assert.equal(result.status, 0, result.stderr);
  return JSON.parse(result.stdout);
}

test('dry-run derives the complete 42-vote expectation and cites runtime evidence', async () => {
  const root = await evidenceRoot();
  const snapshot = fixture();
  const reviewed = runHarness('evidence', snapshot, root);
  const snapshotPath = path.join(root, 'snapshot.json');
  await writeFile(snapshotPath, JSON.stringify(snapshot));

  const result = spawnSync('python3', [
    toolPath,
    '--snapshot', snapshotPath,
    '--repo-root', root,
    '--actor', 'reviewed-operator',
    '--reviewer', 'independent-reviewer',
    '--reason', 'Reviewed historical cluster-card ballot procedure.',
    '--evidence-sha256', reviewed.sha256,
    '--expected-vote-count', '42',
  ], { encoding: 'utf8' });

  assert.equal(result.status, 0, result.stderr);
  const report = JSON.parse(result.stdout);
  assert.equal(report.mode, 'dry-run');
  assert.equal(report.item_count, 21);
  assert.equal(report.expected_vote_count, 42);
  assert.equal(report.planned_resolution_count, 42);
  assert.equal(report.writes_performed, 0);
  assert.equal(report.models[0].votes, 21);
  assert.equal(report.models[1].votes, 21);
  assert.deepEqual(
    report.evidence.procedural_files.map(file => file.path),
    [
      'local/build_llm_vote_packets.py',
      'local/validate_llm_ballots.py',
      'local/submit_llm_ballots.py',
    ],
  );
});

test('planner maps representatives to clusters and none while carrying optimistic guards', async () => {
  const root = await evidenceRoot();
  const snapshot = fixture();
  const result = runHarness('plan', snapshot, root, { expected_vote_count: 42 });
  assert.equal(result.error, undefined);
  assert.equal(result.cluster.p_selection_kind, 'cluster');
  assert.match(result.cluster.p_selection_id, /^cluster-/);
  assert.equal(result.none.p_selection_kind, 'none');
  assert.equal(result.none.p_selection_id, null);
  for (const payload of [result.cluster, result.none]) {
    assert.equal(payload.p_expected_selection_revision, 0);
    assert.match(payload.p_expected_vote_fingerprint_sha256, /^[0-9a-f]{64}$/);
    assert.match(payload.p_evidence_sha256, /^[0-9a-f]{64}$/);
    assert.equal(payload.p_supersedes_resolution_id, null);
    assert.equal(payload.p_actor, 'reviewed-operator');
    assert.equal(payload.p_reviewer, 'independent-reviewer');
    assert.equal(payload.p_evidence_metadata.source_round_id, SOURCE_ROUND);
    assert.equal(payload.p_evidence_metadata.target_round_id, TARGET_ROUND);
  }
});

test('resolution RPCs are never called in dry-run and are called only during apply', async () => {
  const root = await evidenceRoot();
  const result = runHarness('execute', fixture(), root, { expected_vote_count: 42 });
  assert.equal(result.error, undefined);
  assert.equal(result.dry_calls, 0);
  assert.equal(result.dry_report.writes_performed, 0);
  assert.equal(result.apply_calls, 43);
  assert.equal(
    result.rpc_names.filter(name => name === 'resolve_weekly_quiz_vote_selection').length,
    42,
  );
  assert.equal(result.rpc_names.at(-1), 'check_weekly_quiz_selection_provenance');
  assert.equal(result.apply_report.mode, 'apply');
  assert.equal(result.apply_report.writes_performed, 42);
  assert.equal(result.apply_report.post_apply.ready, true);
});

test('planner fails closed on nonrepresentatives, duplicate attempts, drift, scope, and evidence', async () => {
  const root = await evidenceRoot();
  const base = fixture();
  const cases = [];

  const nonrepresentative = structuredClone(base);
  const nonNoneIndex = nonrepresentative.votes.findIndex(vote => !vote.picked_none);
  nonrepresentative.votes[nonNoneIndex].choice_id = 'member-1';
  nonrepresentative.attempts[nonNoneIndex].choice_id = 'member-1';
  nonrepresentative.attempts[nonNoneIndex].app_state.choice_id = 'member-1';
  cases.push([nonrepresentative, /nonrepresentative/]);

  const duplicate = structuredClone(base);
  const duplicateAttempt = structuredClone(duplicate.attempts[0]);
  duplicateAttempt.vote_attempt_id = id(999999);
  duplicate.attempts.push(duplicateAttempt);
  cases.push([duplicate, /is duplicated/]);

  const missingAttempt = structuredClone(base);
  missingAttempt.attempts.shift();
  cases.push([missingAttempt, /is missing/]);

  const timestampDrift = structuredClone(base);
  timestampDrift.votes[0].submitted_at = '2027-01-01T00:00:00+00:00';
  cases.push([timestampDrift, /timestamp changed/]);

  const choiceDrift = structuredClone(base);
  choiceDrift.votes[1].choice_id = 'member-1';
  cases.push([choiceDrift, /choice changed/]);

  const renamed = structuredClone(base);
  renamed.sessions.find(row => row.round_id === TARGET_ROUND).display_name = 'Unexpected Model';
  cases.push([renamed, /missing or renamed/]);

  const unexpectedUser = structuredClone(base);
  unexpectedUser.votes[0].user_id = id(777777);
  cases.push([unexpectedUser, /outside the reviewed allow-list/]);

  const unexpectedItem = structuredClone(base);
  unexpectedItem.votes[0].item_id = 'unexpected-item';
  cases.push([unexpectedItem, /unexpected item/]);

  const incomplete = structuredClone(base);
  incomplete.votes.pop();
  cases.push([incomplete, /count is incomplete/]);

  for (const [snapshot, pattern] of cases) {
    const result = runHarness('plan', snapshot, root, { expected_vote_count: 42 });
    assert.match(result.error, pattern);
  }

  const mismatch = runHarness('plan', base, root, {
    expected_vote_count: 42,
    expected_evidence_sha256: '0'.repeat(64),
  });
  assert.match(mismatch.error, /evidence digest does not match/);
});
