import test from 'node:test';
import assert from 'node:assert/strict';
import {
  deriveReleasedCrystalForItem,
  enrichPrivateWeeklyPool,
  parseTrustedRcsbReferenceUri,
  rcsbReferenceUrl,
  rcsbStructurePageUrl,
  rcsbUncompressedCifUrl,
} from '../lib/released-crystal.js';
import { buildFixture, buildIncompletePoolFixture } from './private-evaluation-fixtures.js';

test('rcsbReferenceUrl returns canonical released mmCIF download URLs', () => {
  assert.equal(rcsbReferenceUrl('9xyz'), 'https://files.rcsb.org/download/9XYZ.cif.gz');
  assert.equal(rcsbUncompressedCifUrl('9XYZ'), 'https://files.rcsb.org/download/9XYZ.cif');
  assert.equal(rcsbStructurePageUrl('9XYZ'), 'https://www.rcsb.org/structure/9XYZ');
});

test('parseTrustedRcsbReferenceUri rejects malicious or mismatched hosts and paths', () => {
  assert.throws(() => parseTrustedRcsbReferenceUri('http://files.rcsb.org/download/9XYZ.cif.gz'), /allow-listed/);
  assert.throws(() => parseTrustedRcsbReferenceUri('https://evil.example/9XYZ.cif.gz'), /allow-listed/);
  assert.throws(() => parseTrustedRcsbReferenceUri('https://files.rcsb.org/view/9XYZ.cif.gz'), /canonical RCSB/);
  assert.throws(
    () => parseTrustedRcsbReferenceUri('https://files.rcsb.org/download/9XYZ.cif.gz?token=1'),
    /query or fragment/,
  );
  assert.equal(parseTrustedRcsbReferenceUri('https://files.rcsb.org/download/9XYZ.cif.gz').pdbId, '9XYZ');
});

test('deriveReleasedCrystalForItem binds ligand identity and canonical RCSB URLs', () => {
  const released = deriveReleasedCrystalForItem({
    itemId: '9XYZ',
    ligand: { component_id: 'DRG', heavy_atoms: 17 },
    revealChoices: [{
      id: 'choice-a',
      reference_uri: 'https://files.rcsb.org/download/9XYZ.cif.gz',
    }, {
      id: 'choice-b',
      reference_uri: 'https://files.rcsb.org/download/9XYZ.cif.gz',
    }],
  });
  assert.equal(released.pdb_id, '9XYZ');
  assert.equal(released.ligand_component_id, 'DRG');
  assert.equal(released.cif_url, 'https://files.rcsb.org/download/9XYZ.cif');
  assert.equal(released.structure_page_url, 'https://www.rcsb.org/structure/9XYZ');
});

test('deriveReleasedCrystalForItem fails closed on URI/target/ligand mismatches', () => {
  const base = {
    itemId: '9XYZ',
    ligand: { component_id: 'DRG' },
    revealChoices: [{ id: 'choice-a', reference_uri: rcsbReferenceUrl('9XYZ') }],
  };
  assert.throws(
    () => deriveReleasedCrystalForItem({
      ...base,
      revealChoices: [{ id: 'choice-a', reference_uri: rcsbReferenceUrl('1ABC') }],
    }),
    /does not match the blind target id/,
  );
  assert.throws(
    () => deriveReleasedCrystalForItem({
      ...base,
      revealChoices: [
        { id: 'choice-a', reference_uri: rcsbReferenceUrl('9XYZ') },
        { id: 'choice-b', reference_uri: rcsbReferenceUrl('1ABC') },
      ],
    }),
    /does not match the blind target id/,
  );
  assert.throws(
    () => deriveReleasedCrystalForItem({ ...base, ligand: null }),
    /blind ligand component identity is missing/,
  );
});

test('enrichPrivateWeeklyPool attaches released crystal metadata and pocket overlays', () => {
  const { bundle } = buildFixture();
  const pool = buildIncompletePoolFixture().pool;
  const enriched = enrichPrivateWeeklyPool(pool, bundle);
  assert.equal(enriched[0].released_crystal.pdb_id, '9XYZ');
  assert.equal(enriched[0].released_crystal.ligand_component_id, 'DRG');
  assert.match(enriched[0].choices[0].answer_crystal_pocket_pdb, /^ATOM/);
});

test('public molecular archive preserves an unscorable item without requiring or fabricating answer overlays', () => {
  const { bundle } = buildFixture();
  const pool = buildIncompletePoolFixture().pool;
  const reveal = bundle.reveal_manifest.items[0];
  Object.assign(reveal, { evaluation_status: 'unscorable', reference_disposition: {
    policy: 'foldarium.released-reference-disposition/v1', code: 'insufficient_reference_coverage', component_id: 'DRG',
    expected_heavy_atoms: 17, observed_heavy_atoms: 12, explicitly_unobserved_heavy_atoms: 5,
    reference_coverage: 12 / 17, minimum_reference_coverage: .8,
  } });
  reveal.choices.forEach(choice => Object.assign(choice, { rmsd: null, correct: null, accepted_correct: null }));
  bundle.answer_overlays = [];
  const enriched = enrichPrivateWeeklyPool(pool, bundle, { publicProjection: true });
  assert.equal(enriched.length, pool.length);
  assert.equal(enriched[0].evaluation_status, 'unscorable');
  assert.equal(enriched[0].has_correct, null);
  assert.equal(enriched[0].answer_overlay, undefined);
  assert.equal(enriched[0].released_crystal.pdb_id, '9XYZ');
  assert.equal(enriched[0].choices.length, pool[0].choices.length);
  for (const [index, choice] of enriched[0].choices.entries()) {
    assert.equal(choice.pose_file, pool[0].choices[index].pose_file);
    assert.equal(choice.rmsd, null);
    assert.equal(choice.correct, null);
    assert.equal(choice.answer_overlay_pdb, undefined);
  }
  assert.throws(() => enrichPrivateWeeklyPool(pool, bundle), /invalid reference disposition/);
  const missing = structuredClone(pool); missing[0].choices.pop();
  assert.throws(() => enrichPrivateWeeklyPool(missing, bundle, { publicProjection: true }), /choices do not match/);
  bundle.answer_overlays = [{ item_id: reveal.id }];
  assert.throws(() => enrichPrivateWeeklyPool(pool, bundle, { publicProjection: true }), /must not have an answer overlay/);
});
