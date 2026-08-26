import test from 'node:test';
import assert from 'node:assert/strict';
import {
  BLINDNESS_ATTESTATION_SCHEMA_VERSION,
  ContractError,
  EMPTY_NETWORK_ALLOWLIST_SHA256,
  KIT_SCHEMA_VERSION,
  SUBMISSION_SCHEMA_VERSION,
  canonicalJson,
  digestSubmission,
  normalizeDisplayName,
  normalizeMethodField,
  sha256Hex,
  validateCompleteSubmission,
  validateBlindnessAttestation,
  validateKitDescriptor,
  validateTokenRequest,
} from '../lib/weekly-selector-contract.js';
import {
  SELECTOR_PROMPT_PROFILE_ID,
  SELECTOR_PROMPT_SHA256,
} from '../lib/weekly-selector-prompt.js';

const BLIND_SHA = 'b'.repeat(64);
const KIT_SHA = 'a'.repeat(64);

const blindManifest = {
  schema_version: 1,
  round_id: 'weekly-2026-08-08',
  items: [
    {
      id: 'item-a',
      choices: [
        { id: 'choice-1', cluster_id: 'cluster-x', is_rep: true },
        { id: 'choice-2', cluster_id: 'cluster-x', is_rep: false },
        { id: 'choice-3', cluster_id: 'cluster-y', is_rep: true },
      ],
    },
    {
      id: 'item-b',
      choices: [
        { id: 'choice-4' },
        { id: 'choice-5' },
      ],
    },
  ],
};

function validKit(overrides = {}) {
  return {
    schema_version: KIT_SCHEMA_VERSION,
    environment: 'preview',
    round_id: 'weekly-2026-08-08',
    blind_manifest_sha256: BLIND_SHA,
    kit_sha256: KIT_SHA,
    item_count: 2,
    byte_size: 4096,
    storage_path: 'selector-kits/weekly-2026-08-08/kit.zip',
    created_at: '2026-08-08T16:00:00.000Z',
    ...overrides,
  };
}

function validSubmission(overrides = {}) {
  return {
    schema_version: SUBMISSION_SCHEMA_VERSION,
    submission_id: '00000000-0000-4000-8000-000000000001',
    environment: 'preview',
    round_id: 'weekly-2026-08-08',
    blind_manifest_sha256: BLIND_SHA,
    kit_sha256: KIT_SHA,
    items: [
      {
        item_id: 'item-a',
        clustered: { selection_kind: 'cluster', cluster_id: 'cluster-x' },
        unclustered: { selection_kind: 'exact', choice_id: 'choice-1' },
      },
      {
        item_id: 'item-b',
        clustered: { selection_kind: 'none' },
        unclustered: { selection_kind: 'none' },
      },
    ],
    ...overrides,
  };
}

function context(overrides = {}) {
  return {
    environment: 'preview',
    roundId: 'weekly-2026-08-08',
    blindManifestSha256: BLIND_SHA,
    kitSha256: KIT_SHA,
    blindManifest,
    ...overrides,
  };
}

function validTokenRequest(overrides = {}) {
  return {
    environment: 'preview',
    round_id: 'weekly-2026-08-08',
    display_name: 'Ada Lovelace',
    method_name: 'fold-ranker',
    method_version: '2.0.0',
    provider: 'example-provider',
    model_name: 'example-model',
    model_version: '2026-08-01',
    prompt_profile_id: SELECTOR_PROMPT_PROFILE_ID,
    prompt_sha256: SELECTOR_PROMPT_SHA256,
    tools_sha256: 'd'.repeat(64),
    config_sha256: 'e'.repeat(64),
    blindness_attestation: validBlindnessAttestation(),
    ...overrides,
  };
}

function validBlindnessAttestation(overrides = {}) {
  return {
    schema_version: BLINDNESS_ATTESTATION_SCHEMA_VERSION,
    workspace_policy: 'verified-kit-only',
    network_policy: 'none',
    network_allowlist_sha256: EMPTY_NETWORK_ALLOWLIST_SHA256,
    browser_enabled: false,
    web_search_enabled: false,
    external_retrieval_enabled: false,
    shared_cache_enabled: false,
    ...overrides,
  };
}

test('validates kit descriptors and rejects unknown or forbidden fields', () => {
  const kit = validateKitDescriptor(validKit());
  assert.equal(kit.schema_version, KIT_SCHEMA_VERSION);
  assert.equal(kit.blind_manifest_sha256, BLIND_SHA);
  assert.throws(
    () => validateKitDescriptor(validKit({ correct: true })),
    ContractError,
  );
  assert.throws(
    () => validateKitDescriptor(validKit({ extra: 'nope' })),
    /unknown key/,
  );
  assert.throws(
    () => validateKitDescriptor(validKit({ kit_sha256: 'not-a-digest' })),
    /kit_sha256/,
  );
  assert.throws(
    () => validateKitDescriptor(validKit({ storage_path: 'selector-kits/../private/index.json' })),
    /storage_path/,
  );
});

test('requires a complete dual-mode submission bound to the round and kit', () => {
  const normalized = validateCompleteSubmission(validSubmission(), context());
  assert.equal(normalized.items[0].clustered.cluster_id, 'cluster-x');
  assert.equal(normalized.items[0].unclustered.choice_id, 'choice-1');
  assert.deepEqual(normalized.items[1].clustered, { selection_kind: 'none' });
  assert.equal(
    digestSubmission(validSubmission(), context()),
    sha256Hex(canonicalJson(normalized)),
  );
});

test('clustered and exact decisions are independent and never inferred from representatives', () => {
  const normalized = validateCompleteSubmission(validSubmission({
    items: [
      {
        item_id: 'item-a',
        clustered: { selection_kind: 'cluster', cluster_id: 'cluster-y' },
        unclustered: { selection_kind: 'exact', choice_id: 'choice-1' },
      },
      validSubmission().items[1],
    ],
  }), context());
  assert.equal(normalized.items[0].clustered.cluster_id, 'cluster-y');
  assert.equal(normalized.items[0].unclustered.choice_id, 'choice-1');

  assert.throws(
    () => validateCompleteSubmission(validSubmission({
      items: [
        {
          item_id: 'item-a',
          clustered: { selection_kind: 'cluster', cluster_id: 'cluster-x' },
          unclustered: {
            selection_kind: 'exact',
            representative_choice_id: 'choice-1',
          },
        },
        validSubmission().items[1],
      ],
    }), context()),
    /forbidden key|unknown key|choice_id/,
  );
});

test('rejects missing, extra, duplicate, and cross-item IDs', () => {
  assert.throws(
    () => validateCompleteSubmission(
      validSubmission({ items: [validSubmission().items[0]] }),
      context(),
    ),
    /per round item|every round item/,
  );
  assert.throws(
    () => validateCompleteSubmission(validSubmission({
      items: [
        ...validSubmission().items,
        {
          item_id: 'item-extra',
          clustered: { selection_kind: 'none' },
          unclustered: { selection_kind: 'none' },
        },
      ],
    }), context()),
    /per round item/,
  );
  assert.throws(
    () => validateCompleteSubmission(validSubmission({
      items: [
        validSubmission().items[0],
        { ...validSubmission().items[1], item_id: 'item-a' },
      ],
    }), context()),
    /duplicate|per round item/,
  );
  assert.throws(
    () => validateCompleteSubmission(validSubmission({
      items: [
        {
          ...validSubmission().items[0],
          unclustered: { selection_kind: 'exact', choice_id: 'choice-4' },
        },
        validSubmission().items[1],
      ],
    }), context()),
    /choice_id is not valid/,
  );
  assert.throws(
    () => validateCompleteSubmission(validSubmission({
      items: [
        validSubmission().items[0],
        {
          item_id: 'item-b',
          clustered: { selection_kind: 'cluster', cluster_id: 'cluster-x' },
          unclustered: { selection_kind: 'none' },
        },
      ],
    }), context()),
    /cluster_id is not valid/,
  );
});

test('rejects bad none identities, reveal fields, v1, and inferred scope', () => {
  const badNone = structuredClone(validSubmission());
  badNone.items[1].clustered.cluster_id = null;
  assert.throws(
    () => validateCompleteSubmission(badNone, context()),
    /unknown key/,
  );
  assert.throws(
    () => validateCompleteSubmission(validSubmission({ answer: 'leak' }), context()),
    /unknown key|forbidden key/,
  );
  assert.throws(
    () => validateCompleteSubmission(validSubmission({ user_id: 'private' }), context()),
    /unknown key|forbidden key/,
  );
  assert.throws(
    () => validateCompleteSubmission(
      validSubmission({ schema_version: 'foldarium.selector-submission/v1' }),
      context(),
    ),
    /unsupported submission/,
  );
  for (const [field, value] of [
    ['environment', 'production'],
    ['round_id', 'another-round'],
    ['blind_manifest_sha256', 'f'.repeat(64)],
    ['kit_sha256', 'f'.repeat(64)],
  ]) {
    assert.throws(
      () => validateCompleteSubmission(validSubmission({ [field]: value }), context()),
      /does not match/,
    );
  }
  assert.throws(
    () => validateCompleteSubmission(validSubmission()),
    /context|required/,
  );
});

test('requires canonical item order and lowercase UUIDs', () => {
  assert.throws(
    () => validateCompleteSubmission(validSubmission({
      items: [...validSubmission().items].reverse(),
    }), context()),
    /canonical item order/,
  );
  assert.throws(
    () => validateCompleteSubmission(validSubmission({
      submission_id: '00000000-0000-4000-8000-0000000000AA',
    }), context()),
    /canonical lowercase UUID/,
  );
});

test('rejects canonical submissions larger than the payload limit', () => {
  const items = Array.from({ length: 600 }, (_, index) => {
    const itemId = `item-${String(index).padStart(4, '0')}-${'x'.repeat(105)}`;
    return {
      manifest: { id: itemId, choices: [] },
      submission: {
        item_id: itemId,
        clustered: { selection_kind: 'none' },
        unclustered: { selection_kind: 'none' },
      },
    };
  });
  assert.throws(
    () => validateCompleteSubmission(validSubmission({
      items: items.map(item => item.submission),
    }), context({
      blindManifest: {
        schema_version: 1,
        round_id: 'weekly-2026-08-08',
        items: items.map(item => item.manifest),
      },
    })),
    /exceeds 65536 bytes/,
  );
});

test('validates the extended model identity and explicit token scope', () => {
  const normalized = validateTokenRequest(
    validTokenRequest({ display_name: '  Ada   Lovelace ' }),
    { environment: 'preview' },
  );
  assert.equal(normalized.display_name, 'Ada Lovelace');
  assert.equal(normalized.provider, 'example-provider');
  assert.equal(normalized.prompt_profile_id, SELECTOR_PROMPT_PROFILE_ID);
  assert.equal(normalized.prompt_sha256, SELECTOR_PROMPT_SHA256);
  assert.deepEqual(normalized.blindness_attestation, validBlindnessAttestation());
  assert.throws(
    () => validateTokenRequest(validTokenRequest({ environment: 'production' }), {
      environment: 'preview',
    }),
    /does not match/,
  );
  assert.throws(
    () => validateTokenRequest(validTokenRequest({ prompt_sha256: 'bad' }), {
      environment: 'preview',
    }),
    /prompt_sha256/,
  );
  assert.throws(
    () => validateTokenRequest(validTokenRequest({ prompt_profile_id: 'custom' }), {
      environment: 'preview',
    }),
    /selector identity/,
  );
  const missingProvider = validTokenRequest();
  delete missingProvider.provider;
  assert.throws(() => validateTokenRequest(missingProvider), /missing required key/);
});

test('blindness attestation rejects unknown keys and enabled capabilities', () => {
  assert.throws(
    () => validateBlindnessAttestation(validBlindnessAttestation({ extra: false })),
    /unknown key/,
  );
  for (const capability of [
    'browser_enabled',
    'web_search_enabled',
    'external_retrieval_enabled',
    'shared_cache_enabled',
  ]) {
    assert.throws(
      () => validateBlindnessAttestation(validBlindnessAttestation({
        [capability]: true,
      })),
      new RegExp(`${capability} must be false`),
    );
  }
});

test('blindness attestation rejects invalid policies and allowlist hashes', () => {
  assert.throws(
    () => validateBlindnessAttestation(validBlindnessAttestation({
      workspace_policy: 'workspace-read-only',
    })),
    /workspace_policy/,
  );
  assert.throws(
    () => validateBlindnessAttestation(validBlindnessAttestation({
      network_policy: 'open',
    })),
    /network_policy/,
  );
  assert.throws(
    () => validateBlindnessAttestation(validBlindnessAttestation({
      network_allowlist_sha256: 'BAD',
    })),
    /lowercase SHA-256/,
  );
  assert.throws(
    () => validateBlindnessAttestation(validBlindnessAttestation({
      network_allowlist_sha256: 'f'.repeat(64),
    })),
    /canonical empty allowlist/,
  );
  assert.throws(
    () => validateBlindnessAttestation(validBlindnessAttestation({
      network_policy: 'provider-api-only',
    })),
    /requires a non-empty allowlist digest/,
  );
  assert.doesNotThrow(() => validateBlindnessAttestation(validBlindnessAttestation({
    network_policy: 'provider-api-only',
    network_allowlist_sha256: 'f'.repeat(64),
  })));
});

test('normalizes display and model identity fields', () => {
  assert.equal(normalizeDisplayName('  Ada   Lovelace '), 'Ada Lovelace');
  assert.equal(normalizeMethodField(' my-method '), 'my-method');
  assert.equal(normalizeDisplayName(''), null);
  assert.equal(normalizeMethodField('\n'), null);
});

test('canonical JSON is stable for digests', () => {
  const left = canonicalJson({ b: 1, a: { d: 2, c: 3 } });
  const right = canonicalJson({ a: { c: 3, d: 2 }, b: 1 });
  assert.equal(left, right);
});
