import { createHash } from 'node:crypto';

export const SELECTOR_PROMPT_PROFILE_SCHEMA_VERSION =
  'foldarium.selector-prompt-profile/v1';
export const SELECTOR_PROMPT_PROFILE_ID = 'weekly-pose-selector-v1';

export const SELECTOR_SYSTEM_PROMPT = `You are Foldarium's blind protein-ligand pose selector.
Use only evidence supplied in the verified Selector kit and the current item packet.
Do not use a browser, web search, external retrieval, prior votes, released/reference/crystal structures, reveal data, or answer-derived information.
Make the clustered and exact-pose decisions independently; never infer either decision from the other.
Return only JSON matching the supplied response schema.
Give brief observable evidence, not hidden chain-of-thought.`;

export const SELECTOR_ITEM_PROMPT_TEMPLATE = `Evaluate blind item {{item_id}}.

Candidate evidence:
{{candidate_evidence_json}}

For clustered mode, choose one advertised cluster_id or choose none only when every cluster is physically implausible.
For exact mode, independently choose one advertised choice_id or choose none only when every individual pose is physically implausible.
A cluster representative is a display member, not an exact-pose choice unless you independently select that same choice_id in exact mode.
Assess steric clashes, ligand burial and pocket occupancy, chemically plausible contacts and hydrogen bonds, receptor-ligand consistency, ligand strain, and unsupported solvent exposure.
Treat pLDDT, Smina affinity, hydrogen-bond counts, and method identity only as weak within-item evidence; these values are not cross-method calibrated and must not override implausible geometry.
Use only identifiers present in this item. Do not fuzzy-match, repair, or invent an identifier.
Return one JSON object and no Markdown.`;

export const SELECTOR_MODEL_RESPONSE_SCHEMA = Object.freeze({
  $schema: 'https://json-schema.org/draft/2020-12/schema',
  title: 'Foldarium blind selector model response',
  type: 'object',
  additionalProperties: false,
  required: ['schema_version', 'item_id', 'clustered', 'unclustered'],
  properties: {
    schema_version: { const: 'foldarium.selector-model-response/v1' },
    item_id: { type: 'string', pattern: '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$' },
    clustered: {
      oneOf: [
        {
          type: 'object',
          additionalProperties: false,
          required: ['selection_kind', 'cluster_id', 'confidence', 'evidence'],
          properties: {
            selection_kind: { const: 'cluster' },
            cluster_id: {
              type: 'string',
              pattern: '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$',
            },
            confidence: { type: 'number', minimum: 0, maximum: 1 },
            evidence: { type: 'string', minLength: 1, maxLength: 240 },
          },
        },
        {
          type: 'object',
          additionalProperties: false,
          required: ['selection_kind', 'confidence', 'evidence'],
          properties: {
            selection_kind: { const: 'none' },
            confidence: { type: 'number', minimum: 0, maximum: 1 },
            evidence: { type: 'string', minLength: 1, maxLength: 240 },
          },
        },
      ],
    },
    unclustered: {
      oneOf: [
        {
          type: 'object',
          additionalProperties: false,
          required: ['selection_kind', 'choice_id', 'confidence', 'evidence'],
          properties: {
            selection_kind: { const: 'exact' },
            choice_id: {
              type: 'string',
              pattern: '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$',
            },
            confidence: { type: 'number', minimum: 0, maximum: 1 },
            evidence: { type: 'string', minLength: 1, maxLength: 240 },
          },
        },
        {
          type: 'object',
          additionalProperties: false,
          required: ['selection_kind', 'confidence', 'evidence'],
          properties: {
            selection_kind: { const: 'none' },
            confidence: { type: 'number', minimum: 0, maximum: 1 },
            evidence: { type: 'string', minLength: 1, maxLength: 240 },
          },
        },
      ],
    },
  },
});

function sortKeys(value) {
  if (Array.isArray(value)) return value.map(sortKeys);
  if (value && typeof value === 'object') {
    return Object.keys(value).sort().reduce((result, key) => {
      result[key] = sortKeys(value[key]);
      return result;
    }, {});
  }
  return value;
}

function canonicalJson(value) {
  return JSON.stringify(sortKeys(value));
}

const PROFILE_BODY = Object.freeze({
  schema_version: SELECTOR_PROMPT_PROFILE_SCHEMA_VERSION,
  prompt_profile_id: SELECTOR_PROMPT_PROFILE_ID,
  system_prompt: SELECTOR_SYSTEM_PROMPT,
  item_prompt_template: SELECTOR_ITEM_PROMPT_TEMPLATE,
  response_schema: SELECTOR_MODEL_RESPONSE_SCHEMA,
});

export const SELECTOR_PROMPT_SHA256 = createHash('sha256')
  .update(canonicalJson(PROFILE_BODY), 'utf8')
  .digest('hex');

export const SELECTOR_PROMPT_PROFILE = Object.freeze({
  ...PROFILE_BODY,
  prompt_sha256: SELECTOR_PROMPT_SHA256,
});
