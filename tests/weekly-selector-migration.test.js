import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const migrationUrl = new URL(
  '../supabase/migrations/20260825223000_upgrade_weekly_selector_v2.sql',
  import.meta.url,
);
const sql = (await readFile(migrationUrl, 'utf8')).replace(/\s+/g, ' ').toLowerCase();

test('v2 migration reconciles additively without rewriting recovered v1 objects', () => {
  assert.match(sql, /additive to the recovered v1 preview schema/);
  assert.match(sql, /create table if not exists public\.weekly_selector_identities_v2/);
  assert.match(sql, /create table if not exists public\.weekly_selector_tokens_v2/);
  assert.match(sql, /create table if not exists public\.weekly_selector_submission_revisions_v2/);
  assert.match(sql, /create table if not exists public\.weekly_selector_submissions_latest_v2/);
  assert.doesNotMatch(sql, /drop table/);
  assert.doesNotMatch(sql, /alter table public\.weekly_selector_identities(?!_v2)/);
  assert.doesNotMatch(sql, /alter table public\.weekly_selector_tokens(?!_v2)/);
  assert.doesNotMatch(sql, /alter table public\.weekly_selector_submission_revisions(?!_v2)/);
});

test('v2 identity and token storage binds complete model and round identity', () => {
  assert.match(sql, /provider text not null/);
  assert.match(sql, /model_name text not null/);
  assert.match(sql, /model_version text not null/);
  assert.match(sql, /prompt_profile_id text not null check \(prompt_profile_id = 'weekly-pose-selector-v1'\)/);
  assert.match(sql, /prompt_sha256 text not null/);
  assert.match(sql, /tools_sha256 text not null/);
  assert.match(sql, /config_sha256 text not null/);
  assert.match(sql, /blindness_attestation jsonb not null/);
  assert.match(sql, /blindness_attestation_sha256 text not null/);
  assert.match(
    sql,
    /prompt_profile_id, prompt_sha256, tools_sha256, config_sha256, blindness_attestation_sha256/,
  );
  assert.match(
    sql,
    /p_prompt_sha256 <> 'e09a6d42af2538ede670dd502ae83f8b6b918e53695b3453ade5e551cfd30f85'/,
  );
  assert.match(sql, /environment text not null check \(environment in \('production', 'preview', 'development'\)\)/);
  assert.match(sql, /round_id text not null references public\.weekly_quiz_rounds/);
  assert.match(sql, /token_hash text not null check \(token_hash ~ '\^\[0-9a-f\]\{64\}\$'\)/);
  assert.match(sql, /expires_at timestamptz not null/);
  assert.match(sql, /revoked_at timestamptz/);
  assert.match(sql, /v_issued_at >= v_round\.closes_at/);
  assert.match(sql, /v_issued_at, v_round\.closes_at/);
  assert.match(sql, /create or replace function public\.revoke_weekly_selector_token_v2/);
  assert.match(sql, /set revoked_at = coalesce\(token\.revoked_at, clock_timestamp\(\)\)/);
  assert.doesNotMatch(sql, /\braw_token\s+text/);
});

test('v2 stores and constrains canonical blindness and network provenance', () => {
  assert.match(
    sql,
    /create or replace function private\.weekly_selector_blindness_attestation_is_valid_v2/,
  );
  assert.match(sql, /select count\(\*\) from jsonb_object_keys\(p_attestation\).* = 8/);
  assert.doesNotMatch(sql, /jsonb_object_length/);
  assert.match(sql, /foldarium\.selector-blindness-attestation\/v1/);
  assert.match(sql, /workspace_policy.*verified-kit-only/);
  assert.match(sql, /network_policy.*\('none', 'provider-api-only'\)/);
  assert.match(
    sql,
    /4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945/,
  );
  assert.match(
    sql,
    /network_policy' <> 'provider-api-only'.*network_allowlist_sha256'.*<> '4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945'/,
  );
  for (const capability of [
    'browser_enabled',
    'web_search_enabled',
    'external_retrieval_enabled',
    'shared_cache_enabled',
  ]) {
    assert.match(sql, new RegExp(`${capability}' = 'false'::jsonb`));
  }
  assert.match(
    sql,
    /extensions\.digest\( convert_to\(private\.weekly_selector_canonical_json\(p_attestation\), 'utf8'\), 'sha256' \)/,
  );
  assert.match(sql, /p_blindness_attestation jsonb/);
  assert.match(sql, /p_blindness_attestation_sha256 text/);
  assert.match(
    sql,
    /p_blindness_attestation, p_blindness_attestation_sha256, private\.foldarium_identity_hmac/,
  );
  assert.match(
    sql,
    /identity\.blindness_attestation, identity\.blindness_attestation_sha256/,
  );
});

test('v2 validates canonical complete dual decisions against exact item scope', () => {
  assert.match(sql, /create or replace function private\.weekly_selector_validate_complete_payload_v2/);
  assert.match(sql, /foldarium\.selector-submission\/v2/);
  assert.match(sql, /submission_id.*environment.*round_id.*blind_manifest_sha256.*kit_sha256.*items/);
  assert.match(sql, /selection_kind.*'cluster'.*cluster_id/);
  assert.match(sql, /selection_kind.*'exact'.*choice_id/);
  assert.match(sql, /none decision must not carry an identity/);
  assert.match(sql, /cluster_id is not valid for this item/);
  assert.match(sql, /choice_id is not valid for this item/);
  assert.match(sql, /items are not canonical/);
  assert.match(sql, /must include every round item exactly once/);
  assert.match(sql, /representative_choice_id/);
  assert.match(sql, /octet_length\(private\.weekly_selector_canonical_json\(p_payload\)\) > 65536/);
});

test('v2 issuance and submission are atomic, idempotent, and pre-close', () => {
  assert.match(sql, /create or replace function public\.issue_weekly_selector_token_v2/);
  assert.match(sql, /v_user_id := auth\.uid\(\)/);
  assert.match(sql, /private\.foldarium_identity_hmac\('participant', v_user_id::text\)/);
  assert.match(sql, /create or replace function public\.submit_weekly_selector_complete_v2/);
  assert.match(sql, /token\.environment = p_environment/);
  assert.match(sql, /token\.round_id = p_round_id/);
  assert.match(sql, /token\.revoked_at is null/);
  assert.match(sql, /selector v2 payload digest does not match canonical payload/);
  assert.match(sql, /v_existing\.payload_digest = p_payload_digest/);
  assert.match(sql, /v_existing\.payload = p_payload/);
  assert.match(sql, /idempotent := true/);
  assert.match(sql, /already bound to a different payload/);
  assert.match(sql, /for update/);
  assert.match(sql, /v_submitted_at := clock_timestamp\(\)/);
  assert.match(sql, /v_submitted_at >= v_round\.closes_at/);
  assert.match(sql, /on conflict \(identity_id, environment, round_id\) do update/);
  assert.match(sql, /revision\.submitted_at < quiz_round\.closes_at/);
});

test('v2 enforces receipt ownership, append-only revisions, RLS, and least privilege', () => {
  assert.match(sql, /create trigger weekly_selector_revisions_v2_append_only/);
  assert.match(sql, /before update or delete on public\.weekly_selector_submission_revisions_v2/);
  assert.match(sql, /create or replace function public\.get_weekly_selector_receipt_v2/);
  assert.match(sql, /token\.identity_id = revision\.identity_id/);
  assert.match(sql, /token\.round_id = revision\.round_id/);
  assert.match(sql, /token\.revoked_at is null/);
  assert.match(sql, /alter table public\.weekly_selector_tokens_v2 enable row level security/);
  assert.match(sql, /alter table public\.weekly_selector_submission_revisions_v2 enable row level security/);
  assert.match(sql, /revoke all on table public\.weekly_selector_tokens_v2 from authenticated/);
  assert.match(sql, /grant execute on function public\.issue_weekly_selector_token_v2/);
  assert.match(sql, /grant execute on function public\.submit_weekly_selector_complete_v2/);
  assert.match(sql, /grant select, insert on table public\.weekly_selector_submission_revisions_v2 to service_role/);
  assert.doesNotMatch(
    sql,
    /grant select, insert, update, delete on table public\.weekly_selector_submission_revisions_v2/,
  );
  assert.doesNotMatch(sql, /alter table public\.weekly_quiz_rounds/);
  assert.doesNotMatch(sql, /drop table public\.weekly_quiz_rounds/);
});
