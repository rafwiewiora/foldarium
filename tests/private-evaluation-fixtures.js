import { createHash } from 'node:crypto';
import {
  ALLOWED_ROUND_ID,
  PRIVATE_EVALUATION_FORMAT_VERSION,
  REVEAL_POLICY_VERSION,
  ACCEPTANCE_POLICY_VERSION,
  CORRECT_RMSD_ANGSTROM,
  canonicalJson,
  hashCanonicalString,
  stableEvaluationId,
} from '../lib/private-evaluation-contract.js';

function pythonLexicalJson(value) {
  return canonicalJson(value).replaceAll('"schema_version":1', '"schema_version":1.0');
}

function manifestCanonicalPair(manifest, pythonFloatLexical) {
  const raw = pythonFloatLexical ? pythonLexicalJson(manifest) : canonicalJson(manifest);
  return { manifest, raw, digest: hashCanonicalString(raw) };
}

function predictionBindingsForReveal(revealManifest) {
  return revealManifest.items.flatMap(item => item.choices.map(choice => ({
    item_id: item.id,
    choice_id: choice.id,
    run_id: choice.run_id,
    sample_id: choice.sample_id,
    prediction_sha256: choice.prediction_sha256,
  }))).sort((left, right) => (
    left.item_id.localeCompare(right.item_id) || left.choice_id.localeCompare(right.choice_id)
  ));
}

function finalizeArtifact({
  artifactObject,
  blindPair,
  revealPair,
  privateIndexSha256,
  references,
  predictionDigest,
  referenceDigest,
}) {
  const artifactBytes = Buffer.from(canonicalJson(artifactObject), 'utf8');
  const artifactSha256 = createHash('sha256').update(artifactBytes).digest('hex');
  const catalog = {
    evaluation_id: stableEvaluationId({
      formatVersion: PRIVATE_EVALUATION_FORMAT_VERSION,
      roundId: ALLOWED_ROUND_ID,
      blindManifestSha256: blindPair.digest,
      privateIndexSha256,
      artifactSha256,
    }),
    round_id: ALLOWED_ROUND_ID,
    campaign_id: 'wwpdb-2026-08-08',
    environment: 'production',
    round_opens_at: '2026-08-14T20:05:00Z',
    round_closes_at: '2026-08-17T20:00:00Z',
    blind_manifest_sha256: blindPair.digest,
    private_index_sha256: privateIndexSha256,
    reveal_manifest_sha256: revealPair.digest,
    reference_set_sha256: referenceDigest,
    prediction_set_sha256: predictionDigest,
    format_version: PRIVATE_EVALUATION_FORMAT_VERSION,
    evaluator_versions: ['test-evaluator/v1'],
    reveal_policy_version: REVEAL_POLICY_VERSION,
    acceptance_policy_version: ACCEPTANCE_POLICY_VERSION,
    correct_rmsd_threshold_angstrom: CORRECT_RMSD_ANGSTROM,
    item_count: artifactObject.counts.item_count,
    choice_count: artifactObject.counts.choice_count,
    artifact_object_uri: `supabase://prediction-results/sha256/${artifactSha256.slice(0, 2)}/${artifactSha256}`,
    artifact_sha256: artifactSha256,
    artifact_size_bytes: artifactBytes.byteLength,
    artifact_media_type: 'application/json',
  };
  return { artifactBytes, catalog, descriptor: { ...catalog } };
}

export function buildFixture({ tamper = null, pythonFloatLexical = false } = {}) {
  const blindManifest = {
    schema_version: 1,
    round_id: ALLOWED_ROUND_ID,
    items: [{
      id: '9XYZ',
      week: '2026-08-08',
      ligand: { component_id: 'DRG', heavy_atoms: 17 },
      protein_uri: 'structures/protein.pdb',
      choices: [{
        id: 'choice-a',
        method: 'openfold3',
        method_version: '0.4.4',
        pose_uri: 'structures/pose-a.pdb',
        cluster_id: 'cluster-a',
        is_rep: true,
        smina_score: {
          metric: 'smina_affinity',
          value: -7.25,
          units: 'kcal/mol',
          protocol: 'score_only',
          scoring_function: 'vina',
        },
      }, {
        id: 'choice-b',
        method: 'boltz2',
        method_version: '2.2.1',
        pose_uri: 'structures/pose-b.pdb',
        cluster_id: 'cluster-a',
        is_rep: false,
        smina_score: {
          metric: 'smina_affinity',
          value: -6.5,
          units: 'kcal/mol',
          protocol: 'score_only',
          scoring_function: 'vina',
        },
      }],
    }],
  };
  const blindPair = manifestCanonicalPair(blindManifest, pythonFloatLexical);
  const privateIndex = {
    schema_version: 1,
    round_id: ALLOWED_ROUND_ID,
    items: blindManifest.items,
  };
  const privateIndexBytes = Buffer.from(canonicalJson(privateIndex), 'utf8');
  const privateIndexSha256 = createHash('sha256').update(privateIndexBytes).digest('hex');
  const references = [{
    item_id: '9XYZ',
    target_id: '9XYZ',
    source_uri: 'https://files.rcsb.org/download/9XYZ.cif.gz',
    sha256: 'aa'.repeat(32),
  }];
  const revealManifest = {
    schema_version: 1,
    round_id: ALLOWED_ROUND_ID,
    blind_manifest_sha256: blindPair.digest,
    items: [{
      id: '9XYZ',
      choices: [{
        id: 'choice-a',
        run_id: 'run-of3',
        sample_id: 'sample-of3-1',
        method: 'openfold3',
        method_version: '0.4.4',
        evaluator_version: 'test-evaluator/v1',
        prediction_sha256: 'bb'.repeat(32),
        reference_uri: references[0].source_uri,
        reference_sha256: references[0].sha256,
        rmsd: 0.8,
        correct: true,
        accepted_correct: true,
      }, {
        id: 'choice-b',
        run_id: 'run-boltz',
        sample_id: 'sample-boltz-1',
        method: 'boltz2',
        method_version: '2.2.1',
        evaluator_version: 'test-evaluator/v1',
        prediction_sha256: 'cc'.repeat(32),
        reference_uri: references[0].source_uri,
        reference_sha256: references[0].sha256,
        rmsd: 4.2,
        correct: false,
        accepted_correct: false,
      }],
    }],
  };
  const revealPair = manifestCanonicalPair(revealManifest, pythonFloatLexical);
  const referenceDigest = hashCanonicalString(canonicalJson(references));
  let predictionDigest = hashCanonicalString(canonicalJson(predictionBindingsForReveal(revealManifest)));
  const overlayPdb = 'HETATM    1 C1   LIG X   1       1.000   2.000   3.000  1.00  0.00           C\nEND\n';
  const overlayPdbB = 'HETATM    1 C1   LIG X   1       4.000   5.000   6.000  1.00  0.00           C\nEND\n';
  const crystalPdb = 'HETATM    1 C1   LIG X   1       1.100   2.100   3.100  1.00  0.00           C\nEND\n';
  const pocketPdb = 'ATOM      1  CA  ALA A   1       1.000   2.000   3.000  1.00  0.00           C\nEND\n';
  const answerOverlays = [{
    item_id: '9XYZ',
    crystal_source_id: 'choice-a',
    crystal_source_rmsd: 0.8,
    crystal_ligand_pdb: crystalPdb,
    crystal_ligand_sha256: createHash('sha256').update(crystalPdb).digest('hex'),
    poses: [{
      id: 'choice-a',
      rmsd: 0.8,
      correct: true,
      predicted_pose_pdb: overlayPdb,
      predicted_pose_sha256: createHash('sha256').update(overlayPdb).digest('hex'),
      crystal_ligand_pdb: crystalPdb,
      crystal_ligand_sha256: createHash('sha256').update(crystalPdb).digest('hex'),
      crystal_pocket_pdb: pocketPdb,
      crystal_pocket_sha256: createHash('sha256').update(pocketPdb).digest('hex'),
    }, {
      id: 'choice-b',
      rmsd: 4.2,
      correct: false,
      predicted_pose_pdb: overlayPdbB,
      predicted_pose_sha256: createHash('sha256').update(overlayPdbB).digest('hex'),
      crystal_ligand_pdb: crystalPdb,
      crystal_ligand_sha256: createHash('sha256').update(crystalPdb).digest('hex'),
      crystal_pocket_pdb: pocketPdb,
      crystal_pocket_sha256: createHash('sha256').update(pocketPdb).digest('hex'),
    }],
  }];
  const answerOverlayDigest = hashCanonicalString(canonicalJson(answerOverlays));
  const artifactObject = {
    format_version: PRIVATE_EVALUATION_FORMAT_VERSION,
    round: {
      round_id: ALLOWED_ROUND_ID,
      campaign_id: 'wwpdb-2026-08-08',
      environment: 'production',
      opens_at: '2026-08-14T20:05:00Z',
      closes_at: '2026-08-17T20:00:00Z',
      blind_manifest_sha256: blindPair.digest,
      private_index: {
        object_uri: `supabase://prediction-results/sha256/${privateIndexSha256.slice(0, 2)}/${privateIndexSha256}`,
        sha256: privateIndexSha256,
      },
    },
    policy: {
      reveal_policy_version: REVEAL_POLICY_VERSION,
      acceptance_policy_version: ACCEPTANCE_POLICY_VERSION,
      correct_rmsd_threshold_angstrom: CORRECT_RMSD_ANGSTROM,
      evaluator_versions: ['test-evaluator/v1'],
    },
    integrity: {
      reveal_manifest_sha256: revealPair.digest,
      reference_set_sha256: referenceDigest,
      prediction_set_sha256: predictionDigest,
      answer_overlay_set_sha256: answerOverlayDigest,
    },
    counts: { item_count: 1, choice_count: 2 },
    references,
    answer_overlays: answerOverlays,
    blind_manifest: blindManifest,
    blind_manifest_canonical_json: blindPair.raw,
    reveal_manifest: revealManifest,
    reveal_manifest_canonical_json: revealPair.raw,
  };
  let { artifactBytes, catalog, descriptor } = finalizeArtifact({
    artifactObject,
    blindPair,
    revealPair,
    privateIndexSha256,
    references,
    predictionDigest,
    referenceDigest,
  });
  const liveRound = {
    round_id: ALLOWED_ROUND_ID,
    campaign_id: 'wwpdb-2026-08-08',
    environment: 'production',
    status: 'open',
    opens_at: '2026-08-14T20:05:00Z',
    closes_at: '2026-08-17T20:00:00Z',
    blind_manifest_sha256: blindPair.digest,
    reveal_manifest: null,
    reveal_manifest_sha256: null,
    revealed_at: null,
    item_count: 1,
    metadata: {
      private_index: {
        object_uri: `supabase://prediction-results/sha256/${privateIndexSha256.slice(0, 2)}/${privateIndexSha256}`,
        sha256: privateIndexSha256,
        media_type: 'application/json',
      },
    },
  };

  if (tamper === 'artifact-bytes') {
    artifactBytes[artifactBytes.length - 2] ^= 0x01;
  }
  if (tamper === 'catalog-blind') {
    catalog.blind_manifest_sha256 = 'ff'.repeat(32);
    descriptor.blind_manifest_sha256 = catalog.blind_manifest_sha256;
  }
  if (tamper === 'descriptor-blind') {
    descriptor.blind_manifest_sha256 = 'ff'.repeat(32);
  }
  if (tamper === 'canonical-blind') {
    artifactObject.blind_manifest_canonical_json = pythonLexicalJson(blindManifest);
    ({ artifactBytes, catalog, descriptor } = finalizeArtifact({
      artifactObject,
      blindPair,
      revealPair,
      privateIndexSha256,
      references,
      predictionDigest,
      referenceDigest,
    }));
  }
  if (tamper === 'canonical-reveal') {
    artifactObject.reveal_manifest_canonical_json = pythonLexicalJson(revealManifest);
    ({ artifactBytes, catalog, descriptor } = finalizeArtifact({
      artifactObject,
      blindPair,
      revealPair,
      privateIndexSha256,
      references,
      predictionDigest,
      referenceDigest,
    }));
  }
  if (tamper === 'reference-digest') {
    descriptor.reference_set_sha256 = 'ff'.repeat(32);
  }
  if (tamper === 'prediction-digest') {
    descriptor.prediction_set_sha256 = 'ff'.repeat(32);
  }
  if (tamper === 'evaluator-mismatch') {
    descriptor.evaluator_versions = ['other-evaluator/v1'];
  }
  if (tamper === 'identity-mismatch') {
    artifactObject.reveal_manifest.items[0].choices[1].id = 'choice-z';
    const mismatchedRevealPair = manifestCanonicalPair(artifactObject.reveal_manifest, false);
    artifactObject.reveal_manifest_canonical_json = mismatchedRevealPair.raw;
    artifactObject.integrity.reveal_manifest_sha256 = mismatchedRevealPair.digest;
    predictionDigest = hashCanonicalString(
      canonicalJson(predictionBindingsForReveal(artifactObject.reveal_manifest)),
    );
    artifactObject.integrity.prediction_set_sha256 = predictionDigest;
    ({ artifactBytes, catalog, descriptor } = finalizeArtifact({
      artifactObject,
      blindPair,
      revealPair: mismatchedRevealPair,
      privateIndexSha256,
      references,
      predictionDigest,
      referenceDigest,
    }));
  }
  if (tamper === 'live-revealed') {
    liveRound.reveal_manifest = revealManifest;
    liveRound.reveal_manifest_sha256 = revealPair.digest;
    liveRound.revealed_at = '2026-08-18T00:00:00Z';
    liveRound.status = 'revealed';
  }
  if (tamper === 'wrong-round') {
    liveRound.round_id = 'weekly-2026-08-15-beta-v5-global-tm-29';
  }
  if (tamper === 'live-blind-json') {
    liveRound.blind_manifest = {
      ...blindManifest,
      items: [{
        ...blindManifest.items[0],
        id: 'WRONG',
      }],
    };
  }

  return {
    catalog,
    descriptor,
    descriptorRaw: JSON.stringify(descriptor),
    liveRound,
    artifactBytes,
    artifactObject,
    revealManifest,
    blindManifest,
    bundle: {
      format_version: PRIVATE_EVALUATION_FORMAT_VERSION,
      round_id: ALLOWED_ROUND_ID,
      campaign_id: catalog.campaign_id,
      opens_at: catalog.round_opens_at,
      closes_at: catalog.round_closes_at,
      evaluation_id: catalog.evaluation_id,
      item_count: 1,
      choice_count: 2,
      blind_manifest: blindManifest,
      blind_manifest_sha256: blindPair.digest,
      reveal_manifest: revealManifest,
      reveal_manifest_sha256: revealPair.digest,
      answer_overlays: answerOverlays.map(row => ({
        item_id: row.item_id,
        crystal_ligand_pdb: row.crystal_ligand_pdb,
        poses: row.poses.map(pose => ({
          id: pose.id,
          rmsd: pose.rmsd,
          correct: pose.correct,
          predicted_pose_pdb: pose.predicted_pose_pdb,
          crystal_ligand_pdb: pose.crystal_ligand_pdb,
          crystal_pocket_pdb: pose.crystal_pocket_pdb,
        })),
      })),
    },
  };
}

export function buildIncompletePoolFixture({ itemCount = 1, choiceCount = 2, missing = null } = {}) {
  const base = buildFixture();
  const bundle = structuredClone(base.bundle);
  const counts = missing === 'item-count'
    ? { itemCount: 29, choiceCount: 58 }
    : { itemCount, choiceCount };
  bundle.item_count = counts.itemCount;
  bundle.choice_count = counts.choiceCount;

  if (missing === 'item-count') {
    return { bundle, pool: base.bundle.blind_manifest.items.map(item => ({
      id: item.id,
      protein_file: item.protein_uri,
      choices: item.choices.map(choice => ({
        id: choice.id,
        _weeklyChoiceId: choice.id,
        pose_file: choice.pose_uri,
        rmsd: 0.8,
        correct: true,
      })),
    })) };
  }

  if (missing === 'choice-id') {
    bundle.reveal_manifest = structuredClone(bundle.reveal_manifest);
    bundle.reveal_manifest.items[0].choices[1].id = 'choice-z';
  }
  if (missing === 'released-crystal') {
    bundle.reveal_manifest = structuredClone(bundle.reveal_manifest);
    bundle.reveal_manifest.items[0].choices[0].reference_uri = 'https://files.rcsb.org/download/1ABC.cif.gz';
  }

  const poolItem = {
    id: '9XYZ',
    ligand: 'DRG',
    protein_file: missing === 'protein' ? '' : 'structures/protein.pdb',
    choices: [{
      id: 'choice-a',
      _weeklyChoiceId: 'choice-a',
      pose_file: missing === 'pose' ? '' : 'structures/pose-a.pdb',
      rmsd: missing === 'rmsd' ? null : 0.8,
      correct: missing === 'correct' ? null : true,
    }, {
      id: 'choice-b',
      _weeklyChoiceId: 'choice-b',
      pose_file: 'structures/pose-b.pdb',
      rmsd: 4.2,
      correct: false,
    }],
  };

  return { bundle, pool: [poolItem] };
}
