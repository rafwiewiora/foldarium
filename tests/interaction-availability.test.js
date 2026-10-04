import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import { projectInteractionCount } from '../lib/interaction-metric.js';
const marker = { metric: 'prolif_hbond_residue_count', value: null, policy: 'prolif-implicit-hbond-unique-protein-residue/v2', status: 'unavailable', availability_policy: 'foldarium.prolif-availability/v1', reason: 'unsupported_receptor_residue', unsupported_residues: ['UNK'] };
test('blind optional metric marker survives archive projection as null', () => {
  assert.deepEqual(projectInteractionCount(marker), marker);
  assert.equal(projectInteractionCount({ metric: marker.metric, policy: marker.policy, value: 0 }).value, 0);
});
test('unavailable evidence cannot carry positions, private text, arbitrary reasons, or numeric scores', () => {
  for (const change of [{ value: 0 }, { unsupported_residues: ['UNK', 'ALA'] }, { reason: 'network error' }, { residue_position: 123 }, { status: 'failed' }]) assert.throws(() => projectInteractionCount({ ...marker, ...change }));
  assert.throws(() => projectInteractionCount({ metric: marker.metric, policy: marker.policy, value: null }));
});
test('both pose evidence labels display unavailable and never coerce null to zero', () => {
  const source = fs.readFileSync(new URL('../app.js', import.meta.url), 'utf8');
  const start = source.indexOf('function weeklyPoseEvidence(choice)');
  const end = source.indexOf('function weeklyEntryEvidence(entry)', start);
  const context = vm.createContext({ methodName: () => 'OpenFold3' });
  vm.runInContext(source.slice(start, end), context);
  context.choice = { _method: 'openfold3', _interactionCount: marker };
  assert.equal(vm.runInContext('weeklyHbondCount(choice)', context), 'H-bonds unavailable');
  assert.equal(vm.runInContext('weeklyPoseEvidence(choice)', context), 'OpenFold3 · H-bonds unavailable');
  context.choice._interactionCount = { metric: marker.metric, value: 0 };
  assert.equal(vm.runInContext('weeklyHbondCount(choice)', context), 'H-bonds 0');
  context.choice._interactionCount = { metric: marker.metric, value: null };
  assert.equal(vm.runInContext('weeklyHbondCount(choice)', context), '');
});
