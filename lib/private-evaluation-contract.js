import { createHash, timingSafeEqual } from 'node:crypto';

export const ALLOWED_ROUND_ID = 'weekly-2026-08-08-beta-v5-global-tm-29';
export const PRIVATE_EVALUATION_FORMAT_VERSION = 'foldarium.weekly-private-evaluation/v5';
export const REVEAL_POLICY_VERSION = 'foldarium-weekly-reveal/v1';
export const ACCEPTANCE_POLICY_VERSION = 'foldarium-weekly-cluster-any-member/v1';
export const CORRECT_RMSD_ANGSTROM = 1.5;

const SHA256 = /^[0-9a-f]{64}$/;

export function canonicalJson(value) {
  return JSON.stringify(sortKeys(value));
}

export function manifestSha256(manifest) {
  return hashCanonicalString(canonicalJson(manifest));
}

export function hashCanonicalString(value) {
  return createHash('sha256').update(String(value), 'utf8').digest('hex');
}

export function stableEvaluationId({
  formatVersion,
  roundId,
  blindManifestSha256,
  privateIndexSha256,
  artifactSha256,
}) {
  return stableId('weekly_eval', {
    format_version: formatVersion,
    round_id: roundId,
    blind_manifest_sha256: blindManifestSha256,
    private_index_sha256: privateIndexSha256,
    artifact_sha256: artifactSha256,
  }, 32);
}

export function parseSupabaseObjectUri(objectUri, expectedSha256 = null) {
  if (typeof objectUri !== 'string') throw new ContractError('object_uri must be text');
  const match = /^supabase:\/\/([^/?#]+)\/sha256\/([0-9a-f]{2})\/([0-9a-f]{64})$/.exec(objectUri);
  if (!match) throw new ContractError('object_uri is not content-addressed');
  const [, bucket, prefix, digest] = match;
  if (prefix !== digest.slice(0, 2)) throw new ContractError('object_uri digest prefix is invalid');
  if (expectedSha256 && expectedSha256 !== digest) {
    throw new ContractError('object_uri digest does not match expected_sha256');
  }
  return { bucket, objectPath: `sha256/${prefix}/${digest}`, digest };
}

export function buildReferenceSet(references) {
  if (!Array.isArray(references) || !references.length) {
    throw new ContractError('references are missing');
  }
  const rows = [];
  const seen = new Set();
  for (const raw of references) {
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
      throw new ContractError('reference row is invalid');
    }
    const itemId = requiredText(raw.item_id, 'reference item_id');
    const targetId = requiredText(raw.target_id, 'reference target_id');
    const sourceUri = requiredText(raw.source_uri, 'reference source_uri');
    const sha256 = requiredSha256(raw.sha256, 'reference sha256');
    if (seen.has(itemId)) throw new ContractError('reference item IDs are duplicated');
    seen.add(itemId);
    rows.push({ item_id: itemId, target_id: targetId, source_uri: sourceUri, sha256 });
  }
  rows.sort((left, right) => left.item_id.localeCompare(right.item_id));
  return rows;
}

export function buildPredictionBindings(revealManifest, referencesByItem) {
  const rawItems = revealManifest?.items;
  if (!Array.isArray(rawItems) || !rawItems.length) {
    throw new ContractError('reveal manifest has no items');
  }
  const bindings = [];
  const seenItems = new Set();
  const seenChoices = new Set();
  for (const rawItem of rawItems) {
    if (!rawItem || typeof rawItem !== 'object' || Array.isArray(rawItem)) {
      throw new ContractError('reveal item is invalid');
    }
    const itemId = requiredText(rawItem.id, 'reveal item id');
    const reference = referencesByItem.get(itemId);
    if (!reference || seenItems.has(itemId)) {
      throw new ContractError('reveal item/reference identities are inconsistent');
    }
    seenItems.add(itemId);
    const choices = rawItem.choices;
    if (!Array.isArray(choices) || !choices.length) {
      throw new ContractError('reveal item has no choices');
    }
    for (const rawChoice of choices) {
      if (!rawChoice || typeof rawChoice !== 'object' || Array.isArray(rawChoice)) {
        throw new ContractError('reveal choice is invalid');
      }
      const choiceId = requiredText(rawChoice.id, 'reveal choice id');
      if (seenChoices.has(choiceId)) throw new ContractError('reveal choice IDs are duplicated');
      seenChoices.add(choiceId);
      if (rawChoice.reference_uri !== reference.source_uri
        || rawChoice.reference_sha256 !== reference.sha256) {
        throw new ContractError('reveal choice is not bound to its released reference');
      }
      bindings.push({
        item_id: itemId,
        choice_id: choiceId,
        run_id: requiredText(rawChoice.run_id, 'choice run_id'),
        sample_id: requiredText(rawChoice.sample_id, 'choice sample_id'),
        prediction_sha256: requiredSha256(rawChoice.prediction_sha256, 'prediction sha256'),
      });
    }
  }
  if (seenItems.size !== referencesByItem.size) {
    throw new ContractError('released references do not exactly match reveal items');
  }
  bindings.sort((left, right) => (
    left.item_id.localeCompare(right.item_id) || left.choice_id.localeCompare(right.choice_id)
  ));
  return bindings;
}

export function buildAnswerOverlays(rawRows, revealManifest) {
  if (!Array.isArray(rawRows)) throw new ContractError('answer overlays are missing');
  const items = revealManifest?.items;
  if (!Array.isArray(items) || !items.length) {
    throw new ContractError('answer overlays require reveal items');
  }
  const expected = new Map(items.map(item => {
    const scored = (item.choices || [])
      .filter(choice => Number.isFinite(choice?.rmsd))
      .sort((left, right) => left.rmsd - right.rmsd
        || String(left.id).localeCompare(String(right.id)));
    if (!scored.length) throw new ContractError('answer overlay reveal choices have no scores');
    return [
      requiredText(item.id, 'answer overlay reveal item_id'),
      { best: scored[0], choices: new Map(scored.map(choice => [choice.id, choice])) },
    ];
  }));
  const rows = [];
  const seen = new Set();
  for (const raw of rawRows) {
    const itemId = requiredText(raw?.item_id, 'answer overlay item_id');
    if (seen.has(itemId) || !expected.has(itemId)) {
      throw new ContractError('answer overlay item identities are inconsistent');
    }
    seen.add(itemId);
    const expectedItem = expected.get(itemId);
    const crystalSourceId = requiredText(
      raw.crystal_source_id, 'answer overlay crystal_source_id',
    );
    if (crystalSourceId !== expectedItem.best.id
      || !Number.isFinite(raw.crystal_source_rmsd)
      || raw.crystal_source_rmsd !== expectedItem.best.rmsd) {
      throw new ContractError('answer overlay crystal binding is inconsistent');
    }
    const crystalPdb = requiredText(
      raw.crystal_ligand_pdb, 'answer overlay crystal ligand PDB',
    );
    if (Buffer.byteLength(crystalPdb, 'utf8') > 1_000_000
      || !crystalPdb.endsWith('\nEND\n')) {
      throw new ContractError('answer overlay crystal ligand PDB is invalid');
    }
    const crystalSha256 = requiredSha256(
      raw.crystal_ligand_sha256, 'answer overlay crystal ligand sha256',
    );
    if (createHash('sha256').update(crystalPdb, 'utf8').digest('hex') !== crystalSha256) {
      throw new ContractError('answer overlay crystal ligand digest is inconsistent');
    }
    if (!Array.isArray(raw.poses)) throw new ContractError('answer overlay poses are missing');
    const poses = [];
    const seenPoses = new Set();
    for (const rawPose of raw.poses) {
      const choiceId = requiredText(rawPose?.id, 'answer overlay pose id');
      const expectedChoice = expectedItem.choices.get(choiceId);
      if (!expectedChoice || seenPoses.has(choiceId)) {
        throw new ContractError('answer overlay pose identities are inconsistent');
      }
      seenPoses.add(choiceId);
      if (!Number.isFinite(rawPose.rmsd)
        || rawPose.rmsd !== expectedChoice.rmsd
        || rawPose.correct !== expectedChoice.correct) {
        throw new ContractError('answer overlay pose score is inconsistent');
      }
      const posePdb = requiredText(rawPose.predicted_pose_pdb, 'answer overlay pose PDB');
      if (Buffer.byteLength(posePdb, 'utf8') > 1_000_000 || !posePdb.endsWith('\nEND\n')) {
        throw new ContractError('answer overlay pose PDB is invalid');
      }
      const poseSha256 = requiredSha256(
        rawPose.predicted_pose_sha256, 'answer overlay pose sha256',
      );
      if (createHash('sha256').update(posePdb, 'utf8').digest('hex') !== poseSha256) {
        throw new ContractError('answer overlay pose digest is inconsistent');
      }
      const poseCrystalPdb = requiredText(
        rawPose.crystal_ligand_pdb, 'answer overlay pose crystal ligand PDB',
      );
      if (Buffer.byteLength(poseCrystalPdb, 'utf8') > 1_000_000
        || !poseCrystalPdb.endsWith('\nEND\n')) {
        throw new ContractError('answer overlay pose crystal ligand PDB is invalid');
      }
      const poseCrystalSha256 = requiredSha256(
        rawPose.crystal_ligand_sha256, 'answer overlay pose crystal ligand sha256',
      );
      if (createHash('sha256').update(poseCrystalPdb, 'utf8').digest('hex')
        !== poseCrystalSha256) {
        throw new ContractError('answer overlay pose crystal ligand digest is inconsistent');
      }
      const posePocketPdb = requiredText(
        rawPose.crystal_pocket_pdb, 'answer overlay pose crystal pocket PDB',
      );
      if (Buffer.byteLength(posePocketPdb, 'utf8') > 1_000_000
        || !posePocketPdb.endsWith('\nEND\n')) {
        throw new ContractError('answer overlay pose crystal pocket PDB is invalid');
      }
      const posePocketSha256 = requiredSha256(
        rawPose.crystal_pocket_sha256, 'answer overlay pose crystal pocket sha256',
      );
      if (createHash('sha256').update(posePocketPdb, 'utf8').digest('hex')
        !== posePocketSha256) {
        throw new ContractError('answer overlay pose crystal pocket digest is inconsistent');
      }
      poses.push({
        id: choiceId,
        rmsd: rawPose.rmsd,
        correct: rawPose.correct,
        predicted_pose_pdb: posePdb,
        predicted_pose_sha256: poseSha256,
        crystal_ligand_pdb: poseCrystalPdb,
        crystal_ligand_sha256: poseCrystalSha256,
        crystal_pocket_pdb: posePocketPdb,
        crystal_pocket_sha256: posePocketSha256,
      });
    }
    if (seenPoses.size !== expectedItem.choices.size) {
      throw new ContractError('answer overlay poses do not exactly cover reveal choices');
    }
    poses.sort((left, right) => left.id.localeCompare(right.id));
    const sourcePose = poses.find(pose => pose.id === crystalSourceId);
    if (sourcePose.crystal_ligand_pdb !== crystalPdb
      || sourcePose.crystal_ligand_sha256 !== crystalSha256) {
      throw new ContractError('answer overlay crystal source ligand is inconsistent');
    }
    rows.push({
      item_id: itemId,
      crystal_source_id: crystalSourceId,
      crystal_source_rmsd: raw.crystal_source_rmsd,
      crystal_ligand_pdb: crystalPdb,
      crystal_ligand_sha256: crystalSha256,
      poses,
    });
  }
  if (seen.size !== expected.size) {
    throw new ContractError('answer overlays do not exactly cover reveal items');
  }
  rows.sort((left, right) => left.item_id.localeCompare(right.item_id));
  return rows;
}

export function verifyLiveRoundState(round) {
  if (!round || typeof round !== 'object') throw new ContractError('live round is missing');
  if (round.round_id !== ALLOWED_ROUND_ID) throw new ContractError('round_id is not allow-listed');
  if (round.environment !== 'production') throw new ContractError('round environment is invalid');
  if (round.reveal_manifest != null
    || round.reveal_manifest_sha256 != null
    || round.revealed_at != null) {
    throw new ContractError('round is already revealed');
  }
  if (round.status !== 'open') throw new ContractError('round is not open');
  const blindDigest = requiredSha256(round.blind_manifest_sha256, 'blind manifest sha256');
  const privateIndex = round.metadata?.private_index;
  const privateDigest = requiredSha256(privateIndex?.sha256, 'private index sha256');
  const privateUri = requiredText(privateIndex?.object_uri, 'private index object_uri');
  parseSupabaseObjectUri(privateUri, privateDigest);
  return {
    roundId: round.round_id,
    campaignId: requiredText(round.campaign_id, 'campaign_id'),
    opensAt: requiredText(round.opens_at, 'opens_at'),
    closesAt: requiredText(round.closes_at, 'closes_at'),
    blindManifestSha256: blindDigest,
    privateIndexSha256: privateDigest,
    itemCount: positiveInt(round.item_count, 'item_count'),
  };
}

export function parseIntegrityDescriptor(raw) {
  if (typeof raw !== 'string' || !raw.trim()) throw new ContractError('descriptor env is missing');
  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch {
    throw new ContractError('descriptor env is not valid JSON');
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new ContractError('descriptor env is invalid');
  }
  return parsed;
}

export function verifyIntegrityDescriptor(descriptor, live) {
  if (!descriptor || typeof descriptor !== 'object') {
    throw new ContractError('integrity descriptor is missing');
  }
  if (descriptor.round_id !== ALLOWED_ROUND_ID) throw new ContractError('descriptor round_id is invalid');
  if (descriptor.format_version !== PRIVATE_EVALUATION_FORMAT_VERSION) {
    throw new ContractError('descriptor format_version is invalid');
  }
  const artifactSha256 = requiredSha256(descriptor.artifact_sha256, 'artifact sha256');
  const expectedEvaluationId = stableEvaluationId({
    formatVersion: descriptor.format_version,
    roundId: descriptor.round_id,
    blindManifestSha256: requiredSha256(descriptor.blind_manifest_sha256, 'descriptor blind digest'),
    privateIndexSha256: requiredSha256(descriptor.private_index_sha256, 'descriptor private index digest'),
    artifactSha256,
  });
  if (descriptor.evaluation_id !== expectedEvaluationId) {
    throw new ContractError('evaluation_id is not deterministic');
  }
  parseSupabaseObjectUri(descriptor.artifact_object_uri, artifactSha256);
  for (const [field, expected] of Object.entries({
    round_id: live.roundId,
    campaign_id: live.campaignId,
    environment: 'production',
    round_opens_at: live.opensAt,
    round_closes_at: live.closesAt,
    blind_manifest_sha256: live.blindManifestSha256,
    private_index_sha256: live.privateIndexSha256,
    reveal_policy_version: REVEAL_POLICY_VERSION,
    acceptance_policy_version: ACCEPTANCE_POLICY_VERSION,
    correct_rmsd_threshold_angstrom: CORRECT_RMSD_ANGSTROM,
    artifact_media_type: 'application/json',
  })) {
    if (descriptor[field] !== expected) {
      throw new ContractError(`descriptor ${field} is not bound to live round`);
    }
  }
  if (descriptor.item_count !== live.itemCount) throw new ContractError('descriptor item_count is invalid');
  if (!Number.isInteger(descriptor.choice_count) || descriptor.choice_count <= 0) {
    throw new ContractError('descriptor choice_count is invalid');
  }
  if (!Number.isInteger(descriptor.artifact_size_bytes) || descriptor.artifact_size_bytes <= 0) {
    throw new ContractError('descriptor artifact_size_bytes is invalid');
  }
  if (!Array.isArray(descriptor.evaluator_versions) || !descriptor.evaluator_versions.length) {
    throw new ContractError('descriptor evaluator_versions is invalid');
  }
  return {
    evaluationId: descriptor.evaluation_id,
    revealManifestSha256: requiredSha256(descriptor.reveal_manifest_sha256, 'descriptor reveal digest'),
    referenceSetSha256: requiredSha256(descriptor.reference_set_sha256, 'descriptor reference digest'),
    predictionSetSha256: requiredSha256(descriptor.prediction_set_sha256, 'descriptor prediction digest'),
    artifactSha256,
    artifactSizeBytes: descriptor.artifact_size_bytes,
    itemCount: descriptor.item_count,
    choiceCount: descriptor.choice_count,
    artifactObjectUri: requiredText(descriptor.artifact_object_uri, 'artifact_object_uri'),
    evaluatorVersions: [...descriptor.evaluator_versions],
  };
}

function manifestIdentity(manifest) {
  const items = new Map();
  if (!manifest || !Array.isArray(manifest.items)) {
    throw new ContractError('manifest item identities are invalid');
  }
  for (const rawItem of manifest.items) {
    const itemId = requiredText(rawItem?.id, 'manifest item id');
    if (items.has(itemId)) throw new ContractError('manifest item IDs are duplicated');
    const choiceIds = new Set();
    if (!Array.isArray(rawItem.choices) || !rawItem.choices.length) {
      throw new ContractError('manifest choice identities are invalid');
    }
    for (const rawChoice of rawItem.choices) {
      const choiceId = requiredText(rawChoice?.id, 'manifest choice id');
      if (choiceIds.has(choiceId)) throw new ContractError('manifest choice IDs are duplicated');
      choiceIds.add(choiceId);
    }
    items.set(itemId, choiceIds);
  }
  return items;
}

function requireManifestCanonicalPair(rawCanonical, parsedObject, field) {
  if (typeof rawCanonical !== 'string' || !rawCanonical) {
    throw new ContractError(`${field} canonical JSON is missing`);
  }
  if (!parsedObject || typeof parsedObject !== 'object' || Array.isArray(parsedObject)) {
    throw new ContractError(`${field} object is missing`);
  }
  let parsedFromCanonical;
  try {
    parsedFromCanonical = JSON.parse(rawCanonical);
  } catch {
    throw new ContractError(`${field} canonical JSON is invalid`);
  }
  if (canonicalJson(parsedFromCanonical) !== canonicalJson(parsedObject)) {
    throw new ContractError(`${field} canonical JSON is inconsistent`);
  }
  return { rawCanonical, parsedFromCanonical };
}

export function verifyArtifactEnvelope(artifactBytes, descriptor, live) {
  if (!Buffer.isBuffer(artifactBytes) || !artifactBytes.length) {
    throw new ContractError('artifact bytes are missing');
  }
  const digest = createHash('sha256').update(artifactBytes).digest('hex');
  if (digest !== descriptor.artifactSha256) throw new ContractError('artifact digest is inconsistent');
  if (artifactBytes.byteLength !== descriptor.artifactSizeBytes) {
    throw new ContractError('artifact size is inconsistent');
  }
  let artifact;
  try {
    artifact = JSON.parse(artifactBytes.toString('utf8'));
  } catch {
    throw new ContractError('artifact is not valid JSON');
  }
  if (artifact?.format_version !== PRIVATE_EVALUATION_FORMAT_VERSION) {
    throw new ContractError('artifact format_version is invalid');
  }
  const round = artifact.round;
  if (!round || typeof round !== 'object') throw new ContractError('artifact round is missing');
  for (const [field, expected] of Object.entries({
    round_id: live.roundId,
    campaign_id: live.campaignId,
    environment: 'production',
    opens_at: live.opensAt,
    closes_at: live.closesAt,
    blind_manifest_sha256: live.blindManifestSha256,
  })) {
    if (round[field] !== expected) throw new ContractError(`artifact round ${field} is invalid`);
  }
  const privateIndex = round.private_index;
  parseSupabaseObjectUri(
    requiredText(privateIndex?.object_uri, 'artifact private index object_uri'),
    requiredSha256(privateIndex?.sha256, 'artifact private index sha256'),
  );
  if (privateIndex.sha256 !== live.privateIndexSha256) {
    throw new ContractError('artifact private index digest is invalid');
  }
  const blindPair = requireManifestCanonicalPair(
    artifact.blind_manifest_canonical_json,
    artifact.blind_manifest,
    'blind_manifest',
  );
  const revealPair = requireManifestCanonicalPair(
    artifact.reveal_manifest_canonical_json,
    artifact.reveal_manifest,
    'reveal_manifest',
  );
  const blindDigest = hashCanonicalString(blindPair.rawCanonical);
  const revealDigest = hashCanonicalString(revealPair.rawCanonical);
  if (blindDigest !== live.blindManifestSha256) {
    throw new ContractError('blind manifest digest is inconsistent');
  }
  if (revealDigest !== descriptor.revealManifestSha256) {
    throw new ContractError('reveal manifest digest is inconsistent');
  }
  const blindManifest = blindPair.parsedFromCanonical;
  const revealManifest = revealPair.parsedFromCanonical;
  if (revealManifest.round_id !== live.roundId) throw new ContractError('reveal round_id is invalid');
  if (revealManifest.blind_manifest_sha256 !== live.blindManifestSha256) {
    throw new ContractError('reveal blind digest is invalid');
  }
  const blindIdentity = manifestIdentity(blindManifest);
  const revealIdentity = manifestIdentity(revealManifest);
  if (blindIdentity.size !== revealIdentity.size) {
    throw new ContractError('blind and reveal item identities differ');
  }
  for (const [itemId, blindChoices] of blindIdentity.entries()) {
    const revealChoices = revealIdentity.get(itemId);
    if (!revealChoices) throw new ContractError('blind and reveal item identities differ');
    if (blindChoices.size !== revealChoices.size) {
      throw new ContractError('blind and reveal choice identities differ');
    }
    for (const choiceId of blindChoices) {
      if (!revealChoices.has(choiceId)) {
        throw new ContractError('blind and reveal choice identities differ');
      }
    }
  }
  const references = buildReferenceSet(artifact.references);
  const referencesByItem = new Map(references.map(row => [row.item_id, row]));
  const predictionBindings = buildPredictionBindings(revealManifest, referencesByItem);
  const answerOverlays = buildAnswerOverlays(artifact.answer_overlays, revealManifest);
  const integrity = artifact.integrity;
  if (!integrity || typeof integrity !== 'object') {
    throw new ContractError('artifact integrity block is missing');
  }
  const referenceDigest = hashCanonicalString(canonicalJson(references));
  const predictionDigest = hashCanonicalString(canonicalJson(predictionBindings));
  const answerOverlayDigest = hashCanonicalString(canonicalJson(answerOverlays));
  if (requiredSha256(integrity.reveal_manifest_sha256, 'artifact reveal digest') !== revealDigest) {
    throw new ContractError('reveal manifest digest is inconsistent');
  }
  if (requiredSha256(integrity.reference_set_sha256, 'artifact reference digest') !== referenceDigest) {
    throw new ContractError('reference set digest is inconsistent');
  }
  if (requiredSha256(integrity.prediction_set_sha256, 'artifact prediction digest') !== predictionDigest) {
    throw new ContractError('prediction set digest is inconsistent');
  }
  if (requiredSha256(
    integrity.answer_overlay_set_sha256, 'artifact answer-overlay digest',
  ) !== answerOverlayDigest) {
    throw new ContractError('answer overlay set digest is inconsistent');
  }
  if (referenceDigest !== descriptor.referenceSetSha256) {
    throw new ContractError('reference set digest is inconsistent');
  }
  if (predictionDigest !== descriptor.predictionSetSha256) {
    throw new ContractError('prediction set digest is inconsistent');
  }
  const itemCount = revealManifest.items.length;
  const choiceCount = predictionBindings.length;
  if (artifact.counts?.item_count !== itemCount || artifact.counts?.choice_count !== choiceCount) {
    throw new ContractError('artifact counts are inconsistent');
  }
  if (itemCount !== descriptor.itemCount || choiceCount !== descriptor.choiceCount) {
    throw new ContractError('artifact counts do not match descriptor');
  }
  if (itemCount !== live.itemCount) throw new ContractError('artifact item_count does not match live round');
  const policy = artifact.policy;
  if (!policy || typeof policy !== 'object') throw new ContractError('artifact policy is missing');
  if (policy.reveal_policy_version !== REVEAL_POLICY_VERSION
    || policy.acceptance_policy_version !== ACCEPTANCE_POLICY_VERSION
    || policy.correct_rmsd_threshold_angstrom !== CORRECT_RMSD_ANGSTROM) {
    throw new ContractError('artifact policy is invalid');
  }
  if (!Array.isArray(policy.evaluator_versions) || !policy.evaluator_versions.length) {
    throw new ContractError('artifact evaluator_versions is invalid');
  }
  if (JSON.stringify(policy.evaluator_versions) !== JSON.stringify(descriptor.evaluatorVersions)) {
    throw new ContractError('evaluator_versions are not bound to artifact policy');
  }
  return {
    blindManifest,
    revealManifest,
    answerOverlays,
    evaluationId: descriptor.evaluationId,
    revealManifestSha256: revealDigest,
  };
}

export function buildClientBundle({
  evaluationId,
  campaignId,
  opensAt,
  closesAt,
  blindManifest,
  blindManifestSha256,
  revealManifest,
  revealManifestSha256,
  answerOverlays,
  itemCount,
  choiceCount,
}) {
  return {
    format_version: PRIVATE_EVALUATION_FORMAT_VERSION,
    round_id: ALLOWED_ROUND_ID,
    campaign_id: campaignId,
    opens_at: opensAt,
    closes_at: closesAt,
    evaluation_id: evaluationId,
    item_count: itemCount,
    choice_count: choiceCount,
    blind_manifest: blindManifest,
    blind_manifest_sha256: blindManifestSha256,
    reveal_manifest: revealManifest,
    reveal_manifest_sha256: revealManifestSha256,
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
  };
}

export function secureEqual(left, right) {
  const digest = value => createHash('sha256').update(String(value)).digest();
  return timingSafeEqual(digest(left), digest(right));
}

export class ContractError extends Error {
  constructor(message) {
    super(message);
    this.name = 'ContractError';
  }
}

function stableId(prefix, value, length) {
  const digest = createHash('sha256').update(canonicalJson(value)).digest('hex');
  return `${prefix}_${digest.slice(0, length)}`;
}

function sortKeys(value) {
  if (Array.isArray(value)) return value.map(sortKeys);
  if (value && typeof value === 'object') {
    return Object.keys(value).sort().reduce((acc, key) => {
      acc[key] = sortKeys(value[key]);
      return acc;
    }, {});
  }
  return value;
}

function requiredText(value, field) {
  if (typeof value !== 'string' || !value) throw new ContractError(`${field} must be non-empty text`);
  return value;
}

function requiredSha256(value, field) {
  if (typeof value !== 'string' || !SHA256.test(value)) {
    throw new ContractError(`${field} must be a lowercase SHA-256`);
  }
  return value;
}

function positiveInt(value, field) {
  if (!Number.isInteger(value) || value <= 0) throw new ContractError(`${field} must be a positive integer`);
  return value;
}
