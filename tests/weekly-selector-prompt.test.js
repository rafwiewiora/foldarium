import test from 'node:test';
import assert from 'node:assert/strict';

import {
  SELECTOR_ITEM_PROMPT_TEMPLATE,
  SELECTOR_MODEL_RESPONSE_SCHEMA,
  SELECTOR_PROMPT_PROFILE,
  SELECTOR_PROMPT_PROFILE_ID,
  SELECTOR_PROMPT_SHA256,
  SELECTOR_SYSTEM_PROMPT,
} from '../lib/weekly-selector-prompt.js';

test('canonical prompt profile is versioned and digest-pinned', () => {
  assert.equal(SELECTOR_PROMPT_PROFILE_ID, 'weekly-pose-selector-v1');
  assert.equal(
    SELECTOR_PROMPT_SHA256,
    'e09a6d42af2538ede670dd502ae83f8b6b918e53695b3453ade5e551cfd30f85',
  );
  assert.equal(SELECTOR_PROMPT_PROFILE.prompt_sha256, SELECTOR_PROMPT_SHA256);
  assert.equal(SELECTOR_PROMPT_PROFILE.system_prompt, SELECTOR_SYSTEM_PROMPT);
  assert.equal(
    SELECTOR_PROMPT_PROFILE.item_prompt_template,
    SELECTOR_ITEM_PROMPT_TEMPLATE,
  );
  assert.deepEqual(
    SELECTOR_PROMPT_PROFILE.response_schema,
    SELECTOR_MODEL_RESPONSE_SCHEMA,
  );
});

test('prompt requires independent blind decisions and strict observable output', () => {
  assert.match(SELECTOR_SYSTEM_PROMPT, /Do not use a browser, web search, external retrieval/);
  assert.match(SELECTOR_SYSTEM_PROMPT, /not hidden chain-of-thought/);
  assert.match(SELECTOR_ITEM_PROMPT_TEMPLATE, /clustered mode/);
  assert.match(SELECTOR_ITEM_PROMPT_TEMPLATE, /exact mode, independently/);
  assert.match(SELECTOR_ITEM_PROMPT_TEMPLATE, /cluster representative is a display member/);
  assert.match(SELECTOR_ITEM_PROMPT_TEMPLATE, /\{\{candidate_evidence_json\}\}/);
  assert.deepEqual(
    SELECTOR_MODEL_RESPONSE_SCHEMA.required,
    ['schema_version', 'item_id', 'clustered', 'unclustered'],
  );
});
