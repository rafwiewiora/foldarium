-- Weekly selector v2. This is additive to the recovered v1 preview schema.
-- V1 tables and RPCs remain available only under their original, unversioned names.

begin;

create or replace function private.weekly_selector_blindness_attestation_is_valid_v2(
  p_attestation jsonb,
  p_attestation_sha256 text
)
returns boolean
language sql
immutable
set search_path = pg_catalog, private, extensions
as $$
  select case
    when jsonb_typeof(p_attestation) is distinct from 'object' then false
    else coalesce(
      (select count(*) from jsonb_object_keys(p_attestation)) = 8
      and p_attestation ?& array[
        'schema_version',
        'workspace_policy',
        'network_policy',
        'network_allowlist_sha256',
        'browser_enabled',
        'web_search_enabled',
        'external_retrieval_enabled',
        'shared_cache_enabled'
      ]
      and p_attestation ->> 'schema_version'
            = 'foldarium.selector-blindness-attestation/v1'
      and p_attestation ->> 'workspace_policy' = 'verified-kit-only'
      and p_attestation ->> 'network_policy' in ('none', 'provider-api-only')
      and jsonb_typeof(p_attestation -> 'network_allowlist_sha256') = 'string'
      and p_attestation ->> 'network_allowlist_sha256' ~ '^[0-9a-f]{64}$'
      and (
        p_attestation ->> 'network_policy' <> 'none'
        or p_attestation ->> 'network_allowlist_sha256'
             = '4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945'
      )
      and (
        p_attestation ->> 'network_policy' <> 'provider-api-only'
        or p_attestation ->> 'network_allowlist_sha256'
             <> '4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945'
      )
      and p_attestation -> 'browser_enabled' = 'false'::jsonb
      and p_attestation -> 'web_search_enabled' = 'false'::jsonb
      and p_attestation -> 'external_retrieval_enabled' = 'false'::jsonb
      and p_attestation -> 'shared_cache_enabled' = 'false'::jsonb
      and p_attestation_sha256 ~ '^[0-9a-f]{64}$'
      and p_attestation_sha256 = encode(
        extensions.digest(
          convert_to(private.weekly_selector_canonical_json(p_attestation), 'UTF8'),
          'sha256'
        ),
        'hex'
      ),
      false
    )
  end
$$;

create table if not exists public.weekly_selector_identities_v2 (
  identity_id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  display_name text not null,
  method_name text not null,
  method_version text not null,
  provider text not null,
  model_name text not null,
  model_version text not null,
  prompt_profile_id text not null
    check (prompt_profile_id = 'weekly-pose-selector-v1'),
  prompt_sha256 text not null check (prompt_sha256 ~ '^[0-9a-f]{64}$'),
  tools_sha256 text not null check (tools_sha256 ~ '^[0-9a-f]{64}$'),
  config_sha256 text not null check (config_sha256 ~ '^[0-9a-f]{64}$'),
  blindness_attestation jsonb not null,
  blindness_attestation_sha256 text not null
    check (blindness_attestation_sha256 ~ '^[0-9a-f]{64}$'),
  participant_hash text not null check (participant_hash ~ '^[0-9a-f]{64}$'),
  display_name_hash text not null check (display_name_hash ~ '^[0-9a-f]{64}$'),
  created_at timestamptz not null default clock_timestamp(),
  unique (
    user_id, display_name, method_name, method_version,
    provider, model_name, model_version,
    prompt_profile_id, prompt_sha256, tools_sha256, config_sha256,
    blindness_attestation_sha256
  ),
  check (
    private.weekly_selector_blindness_attestation_is_valid_v2(
      blindness_attestation,
      blindness_attestation_sha256
    )
  ),
  check (
    display_name = regexp_replace(btrim(display_name), '[[:space:]]+', ' ', 'g')
    and char_length(display_name) between 1 and 80
    and octet_length(display_name) <= 320
    and display_name !~ '[[:cntrl:]]'
  ),
  check (
    char_length(method_name) between 1 and 80
    and char_length(method_version) between 1 and 80
    and char_length(provider) between 1 and 80
    and char_length(model_name) between 1 and 80
    and char_length(model_version) between 1 and 80
    and method_name !~ '[[:cntrl:]]'
    and method_version !~ '[[:cntrl:]]'
    and provider !~ '[[:cntrl:]]'
    and model_name !~ '[[:cntrl:]]'
    and model_version !~ '[[:cntrl:]]'
  )
);

create index if not exists weekly_selector_identities_v2_user_created_idx
  on public.weekly_selector_identities_v2 (user_id, created_at desc);

create table if not exists public.weekly_selector_tokens_v2 (
  token_id uuid primary key default gen_random_uuid(),
  identity_id uuid not null
    references public.weekly_selector_identities_v2(identity_id) on delete cascade,
  environment text not null
    check (environment in ('production', 'preview', 'development')),
  round_id text not null
    references public.weekly_quiz_rounds(round_id) on delete restrict,
  token_hash text not null check (token_hash ~ '^[0-9a-f]{64}$'),
  issued_at timestamptz not null default clock_timestamp(),
  expires_at timestamptz not null,
  revoked_at timestamptz,
  unique (token_hash),
  check (expires_at > issued_at),
  check (revoked_at is null or revoked_at >= issued_at)
);

create index if not exists weekly_selector_tokens_v2_identity_round_idx
  on public.weekly_selector_tokens_v2
  (identity_id, environment, round_id, issued_at desc);

create table if not exists public.weekly_selector_submission_revisions_v2 (
  submission_id uuid not null,
  revision_number integer not null check (revision_number >= 1),
  identity_id uuid not null
    references public.weekly_selector_identities_v2(identity_id) on delete restrict,
  environment text not null
    check (environment in ('production', 'preview', 'development')),
  round_id text not null
    references public.weekly_quiz_rounds(round_id) on delete restrict,
  blind_manifest_sha256 text not null
    check (blind_manifest_sha256 ~ '^[0-9a-f]{64}$'),
  kit_sha256 text not null check (kit_sha256 ~ '^[0-9a-f]{64}$'),
  payload_digest text not null check (payload_digest ~ '^[0-9a-f]{64}$'),
  payload jsonb not null,
  submitted_at timestamptz not null,
  primary key (submission_id, revision_number),
  unique (submission_id),
  check (octet_length(private.weekly_selector_canonical_json(payload)) <= 65536),
  check ((payload ->> 'schema_version') = 'foldarium.selector-submission/v2'),
  check ((payload ->> 'submission_id') = submission_id::text),
  check ((payload ->> 'environment') = environment),
  check ((payload ->> 'round_id') = round_id),
  check ((payload ->> 'blind_manifest_sha256') = blind_manifest_sha256),
  check ((payload ->> 'kit_sha256') = kit_sha256)
);

create unique index if not exists weekly_selector_revisions_v2_identity_round_revision_idx
  on public.weekly_selector_submission_revisions_v2
  (identity_id, environment, round_id, revision_number);
create index if not exists weekly_selector_revisions_v2_identity_round_time_idx
  on public.weekly_selector_submission_revisions_v2
  (identity_id, environment, round_id, submitted_at desc);

create table if not exists public.weekly_selector_submissions_latest_v2 (
  identity_id uuid not null
    references public.weekly_selector_identities_v2(identity_id) on delete restrict,
  environment text not null
    check (environment in ('production', 'preview', 'development')),
  round_id text not null
    references public.weekly_quiz_rounds(round_id) on delete restrict,
  submission_id uuid not null,
  revision_number integer not null check (revision_number >= 1),
  payload_digest text not null check (payload_digest ~ '^[0-9a-f]{64}$'),
  submitted_at timestamptz not null,
  primary key (identity_id, environment, round_id),
  unique (submission_id),
  foreign key (submission_id, revision_number)
    references public.weekly_selector_submission_revisions_v2(
      submission_id, revision_number
    )
    on delete restrict
);

create or replace function private.weekly_selector_reject_revision_mutation_v2()
returns trigger
language plpgsql
set search_path = pg_catalog
as $$
begin
  raise exception 'weekly selector v2 revisions are append-only'
    using errcode = '55000';
end;
$$;

drop trigger if exists weekly_selector_revisions_v2_append_only
  on public.weekly_selector_submission_revisions_v2;
create trigger weekly_selector_revisions_v2_append_only
before update or delete on public.weekly_selector_submission_revisions_v2
for each row execute function private.weekly_selector_reject_revision_mutation_v2();

create or replace function private.weekly_selector_validate_complete_payload_v2(
  p_payload jsonb,
  p_submission_id uuid,
  p_environment text,
  p_round_id text,
  p_blind_manifest_sha256 text,
  p_kit_sha256 text,
  p_blind_manifest jsonb,
  p_expected_item_count integer
)
returns void
language plpgsql
stable
set search_path = pg_catalog, private
as $$
declare
  v_entry jsonb;
  v_item_id text;
  v_previous_item_id text;
  v_manifest_item jsonb;
  v_clustered jsonb;
  v_unclustered jsonb;
  v_cluster_id text;
  v_choice_id text;
  v_seen_items text[] := array[]::text[];
  v_item_count integer := 0;
begin
  if p_payload is null
     or jsonb_typeof(p_payload) is distinct from 'object'
     or (select count(*) from jsonb_object_keys(p_payload)) <> 7
     or not (p_payload ?& array[
       'schema_version', 'submission_id', 'environment', 'round_id',
       'blind_manifest_sha256', 'kit_sha256', 'items'
     ])
     or p_payload ->> 'schema_version'
          is distinct from 'foldarium.selector-submission/v2'
     or p_payload ->> 'submission_id' is distinct from p_submission_id::text
     or p_payload ->> 'environment' is distinct from p_environment
     or p_payload ->> 'round_id' is distinct from p_round_id
     or p_payload ->> 'blind_manifest_sha256'
          is distinct from p_blind_manifest_sha256
     or p_payload ->> 'kit_sha256' is distinct from p_kit_sha256
     or jsonb_typeof(p_payload -> 'items') is distinct from 'array'
     or jsonb_array_length(p_payload -> 'items') = 0
     or octet_length(private.weekly_selector_canonical_json(p_payload)) > 65536
     or private.weekly_selector_payload_has_forbidden_keys(
       p_payload,
       array[
         'correct', 'accepted_correct', 'rmsd', 'answer', 'answer_metadata',
         'score', 'reference', 'crystal', 'run_id', 'sample_id',
         'artifact_sha256', 'private_index', 'reveal_manifest', 'coordinates',
         'user_id', 'token_hash', 'participant_hash', 'display_name_hash',
         'representative_id', 'representative_choice_id', 'is_rep'
       ]
     ) then
    raise exception 'selector v2 submission payload is invalid'
      using errcode = '22023';
  end if;

  if jsonb_typeof(p_blind_manifest) is distinct from 'object'
     or jsonb_typeof(p_blind_manifest -> 'items') is distinct from 'array'
     or jsonb_array_length(p_blind_manifest -> 'items')
          is distinct from p_expected_item_count then
    raise exception 'selector v2 trusted blind manifest is invalid'
      using errcode = '22023';
  end if;

  for v_entry in
    select value
      from jsonb_array_elements(p_payload -> 'items') with ordinality
        as submitted(value, ordinality)
     order by ordinality
  loop
    v_item_count := v_item_count + 1;
    if jsonb_typeof(v_entry) is distinct from 'object'
       or (select count(*) from jsonb_object_keys(v_entry)) <> 3
       or not (v_entry ?& array['item_id', 'clustered', 'unclustered']) then
      raise exception 'selector v2 submission item shape is invalid'
        using errcode = '22023';
    end if;

    v_item_id := v_entry ->> 'item_id';
    if v_item_id is null
       or v_item_id !~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'
       or v_item_id = any (v_seen_items) then
      raise exception 'selector v2 submission item_id is invalid or duplicate'
        using errcode = '22023';
    end if;
    if v_previous_item_id is not null
       and v_item_id collate "C" <= v_previous_item_id collate "C" then
      raise exception 'selector v2 submission items are not canonical'
        using errcode = '22023';
    end if;
    v_previous_item_id := v_item_id;
    v_seen_items := array_append(v_seen_items, v_item_id);

    select item.value
      into v_manifest_item
      from jsonb_array_elements(p_blind_manifest -> 'items') as item(value)
     where item.value ->> 'id' = v_item_id
     limit 1;
    if v_manifest_item is null then
      raise exception 'selector v2 item_id is not in the blind manifest'
        using errcode = '22023';
    end if;

    v_clustered := v_entry -> 'clustered';
    if jsonb_typeof(v_clustered) is distinct from 'object' then
      raise exception 'selector v2 clustered decision must be an object'
        using errcode = '22023';
    elsif v_clustered ->> 'selection_kind' = 'none' then
      if (select count(*) from jsonb_object_keys(v_clustered)) <> 1
         or not (v_clustered ? 'selection_kind') then
        raise exception 'selector v2 none decision must not carry an identity'
          using errcode = '22023';
      end if;
    elsif v_clustered ->> 'selection_kind' = 'cluster' then
      if (select count(*) from jsonb_object_keys(v_clustered)) <> 2
         or not (v_clustered ?& array['selection_kind', 'cluster_id'])
         or jsonb_typeof(v_clustered -> 'cluster_id') is distinct from 'string'
         or (v_clustered ->> 'cluster_id')
              !~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$' then
        raise exception 'selector v2 cluster decision is invalid'
          using errcode = '22023';
      end if;
      v_cluster_id := v_clustered ->> 'cluster_id';
      if not exists (
        select 1
          from jsonb_array_elements(v_manifest_item -> 'choices') as choice(value)
         where choice.value ->> 'cluster_id' = v_cluster_id
      ) then
        raise exception 'selector v2 cluster_id is not valid for this item'
          using errcode = '22023';
      end if;
    else
      raise exception 'selector v2 clustered selection_kind is invalid'
        using errcode = '22023';
    end if;

    v_unclustered := v_entry -> 'unclustered';
    if jsonb_typeof(v_unclustered) is distinct from 'object' then
      raise exception 'selector v2 unclustered decision must be an object'
        using errcode = '22023';
    elsif v_unclustered ->> 'selection_kind' = 'none' then
      if (select count(*) from jsonb_object_keys(v_unclustered)) <> 1
         or not (v_unclustered ? 'selection_kind') then
        raise exception 'selector v2 none decision must not carry an identity'
          using errcode = '22023';
      end if;
    elsif v_unclustered ->> 'selection_kind' = 'exact' then
      if (select count(*) from jsonb_object_keys(v_unclustered)) <> 2
         or not (v_unclustered ?& array['selection_kind', 'choice_id'])
         or jsonb_typeof(v_unclustered -> 'choice_id') is distinct from 'string'
         or (v_unclustered ->> 'choice_id')
              !~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$' then
        raise exception 'selector v2 exact decision is invalid'
          using errcode = '22023';
      end if;
      v_choice_id := v_unclustered ->> 'choice_id';
      if not exists (
        select 1
          from jsonb_array_elements(v_manifest_item -> 'choices') as choice(value)
         where choice.value ->> 'id' = v_choice_id
      ) then
        raise exception 'selector v2 choice_id is not valid for this item'
          using errcode = '22023';
      end if;
    else
      raise exception 'selector v2 unclustered selection_kind is invalid'
        using errcode = '22023';
    end if;
  end loop;

  if v_item_count <> p_expected_item_count
     or exists (
       select 1
         from jsonb_array_elements(p_blind_manifest -> 'items') as item(value)
        where not ((item.value ->> 'id') = any (v_seen_items))
     ) then
    raise exception 'selector v2 submission must include every round item exactly once'
      using errcode = '22023';
  end if;
end;
$$;

create or replace function public.get_weekly_selector_round_v2(
  p_environment text,
  p_round_id text
)
returns table (
  environment text,
  round_id text,
  public_status text,
  opens_at timestamptz,
  closes_at timestamptz,
  item_count integer,
  blind_manifest jsonb,
  blind_manifest_sha256 text
)
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
  select
    quiz_round.environment,
    quiz_round.round_id,
    case
      when quiz_round.status = 'open'
       and clock_timestamp() >= quiz_round.opens_at
       and clock_timestamp() < quiz_round.closes_at then 'open'
      else 'closed'
    end,
    quiz_round.opens_at,
    quiz_round.closes_at,
    quiz_round.item_count,
    quiz_round.blind_manifest,
    quiz_round.blind_manifest_sha256
  from public.weekly_quiz_rounds as quiz_round
  where quiz_round.environment = p_environment
    and quiz_round.round_id = p_round_id
    and p_environment in ('production', 'preview', 'development')
    and quiz_round.blind_manifest is not null
    and quiz_round.blind_manifest_sha256 is not null
  limit 1
$$;

create or replace function public.issue_weekly_selector_token_v2(
  p_environment text,
  p_round_id text,
  p_display_name text,
  p_method_name text,
  p_method_version text,
  p_provider text,
  p_model_name text,
  p_model_version text,
  p_prompt_profile_id text,
  p_prompt_sha256 text,
  p_tools_sha256 text,
  p_config_sha256 text,
  p_blindness_attestation jsonb,
  p_blindness_attestation_sha256 text,
  p_token_hash text
)
returns table (
  token_id uuid,
  identity_id uuid,
  round_id text,
  expires_at timestamptz,
  issued_at timestamptz
)
language plpgsql
security definer
set search_path = pg_catalog, public, private
as $$
declare
  v_user_id uuid;
  v_display_name text;
  v_method_name text;
  v_method_version text;
  v_provider text;
  v_model_name text;
  v_model_version text;
  v_round public.weekly_quiz_rounds%rowtype;
  v_identity_id uuid;
  v_token_id uuid;
  v_issued_at timestamptz;
begin
  v_user_id := auth.uid();
  if v_user_id is null then
    raise exception 'selector v2 token issuance requires authentication'
      using errcode = '42501';
  end if;
  if p_environment not in ('production', 'preview', 'development')
     or p_round_id !~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'
     or p_token_hash !~ '^[0-9a-f]{64}$'
     or p_prompt_profile_id <> 'weekly-pose-selector-v1'
     or p_prompt_sha256 <> 'e09a6d42af2538ede670dd502ae83f8b6b918e53695b3453ade5e551cfd30f85'
     or p_tools_sha256 !~ '^[0-9a-f]{64}$'
     or p_config_sha256 !~ '^[0-9a-f]{64}$'
     or not private.weekly_selector_blindness_attestation_is_valid_v2(
       p_blindness_attestation,
       p_blindness_attestation_sha256
     ) then
    raise exception 'invalid selector v2 token issuance request'
      using errcode = '22023';
  end if;

  v_display_name := regexp_replace(btrim(p_display_name), '[[:space:]]+', ' ', 'g');
  if nullif(v_display_name, '') is null
     or char_length(v_display_name) > 80
     or octet_length(v_display_name) > 320
     or v_display_name ~ '[[:cntrl:]]' then
    raise exception 'invalid selector v2 display name'
      using errcode = '22023';
  end if;
  v_method_name := private.weekly_selector_normalize_method(p_method_name, 'method_name');
  v_method_version := private.weekly_selector_normalize_method(p_method_version, 'method_version');
  v_provider := private.weekly_selector_normalize_method(p_provider, 'provider');
  v_model_name := private.weekly_selector_normalize_method(p_model_name, 'model_name');
  v_model_version := private.weekly_selector_normalize_method(p_model_version, 'model_version');

  select quiz_round.*
    into v_round
    from public.weekly_quiz_rounds as quiz_round
   where quiz_round.round_id = p_round_id
     and quiz_round.environment = p_environment
     and quiz_round.status = 'open'
     and clock_timestamp() >= quiz_round.opens_at
     and clock_timestamp() < quiz_round.closes_at
   for share;
  if not found then
    raise exception 'selector v2 token round is not open'
      using errcode = '22023';
  end if;

  insert into public.weekly_selector_identities_v2 (
    user_id, display_name, method_name, method_version,
    provider, model_name, model_version,
    prompt_profile_id, prompt_sha256, tools_sha256, config_sha256,
    blindness_attestation, blindness_attestation_sha256,
    participant_hash, display_name_hash
  )
  values (
    v_user_id, v_display_name, v_method_name, v_method_version,
    v_provider, v_model_name, v_model_version,
    p_prompt_profile_id, p_prompt_sha256, p_tools_sha256, p_config_sha256,
    p_blindness_attestation, p_blindness_attestation_sha256,
    private.foldarium_identity_hmac('participant', v_user_id::text),
    private.foldarium_identity_hmac(
      'display-name', v_user_id::text || ':' || lower(v_display_name)
    )
  )
  on conflict (
    user_id, display_name, method_name, method_version,
    provider, model_name, model_version,
    prompt_profile_id, prompt_sha256, tools_sha256, config_sha256,
    blindness_attestation_sha256
  )
  do update set display_name = excluded.display_name
  returning weekly_selector_identities_v2.identity_id into v_identity_id;

  v_issued_at := clock_timestamp();
  if v_issued_at >= v_round.closes_at then
    raise exception 'selector v2 token round closed before issuance commit'
      using errcode = '22023';
  end if;

  insert into public.weekly_selector_tokens_v2 (
    identity_id, environment, round_id, token_hash, issued_at, expires_at
  )
  values (
    v_identity_id, p_environment, p_round_id, p_token_hash,
    v_issued_at, v_round.closes_at
  )
  returning weekly_selector_tokens_v2.token_id into v_token_id;

  token_id := v_token_id;
  identity_id := v_identity_id;
  round_id := p_round_id;
  expires_at := v_round.closes_at;
  issued_at := v_issued_at;
  return next;
end;
$$;

create or replace function public.revoke_weekly_selector_token_v2(
  p_token_id uuid,
  p_environment text
)
returns table (
  token_id uuid,
  revoked_at timestamptz
)
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_user_id uuid;
begin
  v_user_id := auth.uid();
  if v_user_id is null then
    raise exception 'selector v2 token revocation requires authentication'
      using errcode = '42501';
  end if;
  if p_token_id is null
     or p_environment not in ('production', 'preview', 'development') then
    raise exception 'invalid selector v2 token revocation request'
      using errcode = '22023';
  end if;

  update public.weekly_selector_tokens_v2 as token
     set revoked_at = coalesce(token.revoked_at, clock_timestamp())
    from public.weekly_selector_identities_v2 as identity
   where token.token_id = p_token_id
     and token.environment = p_environment
     and identity.identity_id = token.identity_id
     and identity.user_id = v_user_id
  returning token.token_id, token.revoked_at
  into token_id, revoked_at;
  if found then return next; end if;
end;
$$;

create or replace function public.submit_weekly_selector_complete_v2(
  p_token_hash text,
  p_environment text,
  p_round_id text,
  p_submission_id uuid,
  p_blind_manifest_sha256 text,
  p_kit_sha256 text,
  p_payload jsonb,
  p_payload_digest text
)
returns table (
  submission_id uuid,
  revision_number integer,
  environment text,
  round_id text,
  blind_manifest_sha256 text,
  kit_sha256 text,
  payload_digest text,
  submitted_at timestamptz,
  idempotent boolean
)
language plpgsql
security definer
set search_path = pg_catalog, public, private, extensions
as $$
declare
  v_token public.weekly_selector_tokens_v2%rowtype;
  v_round public.weekly_quiz_rounds%rowtype;
  v_kit private.weekly_selector_kit_catalog%rowtype;
  v_existing public.weekly_selector_submission_revisions_v2%rowtype;
  v_revision_number integer;
  v_computed_digest text;
  v_submitted_at timestamptz;
begin
  if p_token_hash !~ '^[0-9a-f]{64}$'
     or p_environment not in ('production', 'preview', 'development')
     or p_round_id !~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'
     or p_submission_id is null
     or p_blind_manifest_sha256 !~ '^[0-9a-f]{64}$'
     or p_kit_sha256 !~ '^[0-9a-f]{64}$'
     or p_payload_digest !~ '^[0-9a-f]{64}$'
     or p_payload is null then
    raise exception 'invalid selector v2 submission request'
      using errcode = '22023';
  end if;

  v_computed_digest := encode(
    extensions.digest(
      convert_to(private.weekly_selector_canonical_json(p_payload), 'UTF8'),
      'sha256'
    ),
    'hex'
  );
  if p_payload_digest is distinct from v_computed_digest then
    raise exception 'selector v2 payload digest does not match canonical payload'
      using errcode = '22023';
  end if;

  select token.*
    into v_token
    from public.weekly_selector_tokens_v2 as token
   where token.token_hash = p_token_hash
     and token.environment = p_environment
     and token.round_id = p_round_id
     and token.revoked_at is null
   limit 1;
  if not found then
    raise exception 'selector v2 bearer token is invalid or revoked'
      using errcode = '42501';
  end if;

  select revision.*
    into v_existing
    from public.weekly_selector_submission_revisions_v2 as revision
   where revision.submission_id = p_submission_id
   limit 1;
  if found then
    if v_existing.identity_id = v_token.identity_id
       and v_existing.environment = p_environment
       and v_existing.round_id = p_round_id
       and v_existing.blind_manifest_sha256 = p_blind_manifest_sha256
       and v_existing.kit_sha256 = p_kit_sha256
       and v_existing.payload_digest = p_payload_digest
       and v_existing.payload = p_payload then
      submission_id := v_existing.submission_id;
      revision_number := v_existing.revision_number;
      environment := v_existing.environment;
      round_id := v_existing.round_id;
      blind_manifest_sha256 := v_existing.blind_manifest_sha256;
      kit_sha256 := v_existing.kit_sha256;
      payload_digest := v_existing.payload_digest;
      submitted_at := v_existing.submitted_at;
      idempotent := true;
      return next;
      return;
    end if;
    raise exception 'selector v2 submission id is already bound to a different payload'
      using errcode = '23505';
  end if;

  if clock_timestamp() >= v_token.expires_at then
    raise exception 'selector v2 bearer token is expired'
      using errcode = '42501';
  end if;

  select quiz_round.*
    into v_round
    from public.weekly_quiz_rounds as quiz_round
   where quiz_round.round_id = p_round_id
     and quiz_round.environment = p_environment
     and quiz_round.status = 'open'
     and clock_timestamp() >= quiz_round.opens_at
     and clock_timestamp() < quiz_round.closes_at
   for share;
  if not found then
    raise exception 'selector v2 round is not open'
      using errcode = '22023';
  end if;

  select kit.*
    into v_kit
    from private.weekly_selector_kit_catalog as kit
   where kit.round_id = p_round_id
     and kit.kit_sha256 = p_kit_sha256
     and kit.blind_manifest_sha256 = p_blind_manifest_sha256
   limit 1;
  if not found
     or v_round.blind_manifest_sha256 is distinct from p_blind_manifest_sha256
     or v_round.item_count is distinct from v_kit.item_count then
    raise exception 'selector v2 round, blind manifest, and kit binding is invalid'
      using errcode = '22023';
  end if;

  perform private.weekly_selector_validate_complete_payload_v2(
    p_payload,
    p_submission_id,
    p_environment,
    p_round_id,
    p_blind_manifest_sha256,
    p_kit_sha256,
    v_round.blind_manifest,
    v_round.item_count
  );

  perform 1
    from public.weekly_selector_identities_v2 as identity
   where identity.identity_id = v_token.identity_id
   for update;

  v_submitted_at := clock_timestamp();
  if v_submitted_at >= v_round.closes_at then
    raise exception 'selector v2 round closed before submission commit'
      using errcode = '22023';
  end if;

  select coalesce(max(revision.revision_number), 0) + 1
    into v_revision_number
    from public.weekly_selector_submission_revisions_v2 as revision
   where revision.identity_id = v_token.identity_id
     and revision.environment = p_environment
     and revision.round_id = p_round_id;

  insert into public.weekly_selector_submission_revisions_v2 (
    submission_id, revision_number, identity_id, environment, round_id,
    blind_manifest_sha256, kit_sha256, payload_digest, payload, submitted_at
  )
  values (
    p_submission_id, v_revision_number, v_token.identity_id,
    p_environment, p_round_id, p_blind_manifest_sha256, p_kit_sha256,
    p_payload_digest, p_payload, v_submitted_at
  );

  insert into public.weekly_selector_submissions_latest_v2 (
    identity_id, environment, round_id, submission_id,
    revision_number, payload_digest, submitted_at
  )
  values (
    v_token.identity_id, p_environment, p_round_id, p_submission_id,
    v_revision_number, p_payload_digest, v_submitted_at
  )
  on conflict (identity_id, environment, round_id)
  do update set
    submission_id = excluded.submission_id,
    revision_number = excluded.revision_number,
    payload_digest = excluded.payload_digest,
    submitted_at = excluded.submitted_at;

  submission_id := p_submission_id;
  revision_number := v_revision_number;
  environment := p_environment;
  round_id := p_round_id;
  blind_manifest_sha256 := p_blind_manifest_sha256;
  kit_sha256 := p_kit_sha256;
  payload_digest := p_payload_digest;
  submitted_at := v_submitted_at;
  idempotent := false;
  return next;
end;
$$;

create or replace function public.get_weekly_selector_receipt_v2(
  p_submission_id uuid,
  p_token_hash text,
  p_environment text
)
returns table (
  submission_id uuid,
  revision_number integer,
  environment text,
  round_id text,
  blind_manifest_sha256 text,
  kit_sha256 text,
  payload_digest text,
  submitted_at timestamptz
)
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
  select
    revision.submission_id,
    revision.revision_number,
    revision.environment,
    revision.round_id,
    revision.blind_manifest_sha256,
    revision.kit_sha256,
    revision.payload_digest,
    revision.submitted_at
  from public.weekly_selector_submission_revisions_v2 as revision
  join public.weekly_selector_tokens_v2 as token
    on token.identity_id = revision.identity_id
   and token.token_hash = p_token_hash
   and token.environment = revision.environment
   and token.round_id = revision.round_id
   and token.revoked_at is null
  where revision.submission_id = p_submission_id
    and revision.environment = p_environment
    and p_environment in ('production', 'preview', 'development')
  limit 1
$$;

create or replace function public.get_weekly_selector_latest_submissions_v2(
  p_environment text,
  p_round_id text
)
returns table (
  environment text,
  round_id text,
  payload jsonb,
  display_name text,
  method_name text,
  method_version text,
  provider text,
  model_name text,
  model_version text,
  prompt_profile_id text,
  prompt_sha256 text,
  tools_sha256 text,
  config_sha256 text,
  blindness_attestation jsonb,
  blindness_attestation_sha256 text
)
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
  select
    latest.environment,
    latest.round_id,
    revision.payload,
    identity.display_name,
    identity.method_name,
    identity.method_version,
    identity.provider,
    identity.model_name,
    identity.model_version,
    identity.prompt_profile_id,
    identity.prompt_sha256,
    identity.tools_sha256,
    identity.config_sha256,
    identity.blindness_attestation,
    identity.blindness_attestation_sha256
  from public.weekly_selector_submissions_latest_v2 as latest
  join public.weekly_selector_submission_revisions_v2 as revision
    on revision.submission_id = latest.submission_id
   and revision.revision_number = latest.revision_number
  join public.weekly_selector_identities_v2 as identity
    on identity.identity_id = latest.identity_id
  join public.weekly_quiz_rounds as quiz_round
    on quiz_round.round_id = latest.round_id
   and quiz_round.environment = latest.environment
  where latest.environment = p_environment
    and latest.round_id = p_round_id
    and quiz_round.status = 'revealed'
    and revision.submitted_at < quiz_round.closes_at
  order by
    lower(identity.display_name),
    lower(identity.method_name),
    lower(identity.method_version),
    identity.identity_id
$$;

alter table public.weekly_selector_identities_v2 enable row level security;
alter table public.weekly_selector_tokens_v2 enable row level security;
alter table public.weekly_selector_submission_revisions_v2 enable row level security;
alter table public.weekly_selector_submissions_latest_v2 enable row level security;

revoke all on table public.weekly_selector_identities_v2 from public;
revoke all on table public.weekly_selector_tokens_v2 from public;
revoke all on table public.weekly_selector_submission_revisions_v2 from public;
revoke all on table public.weekly_selector_submissions_latest_v2 from public;
revoke all on function private.weekly_selector_blindness_attestation_is_valid_v2(
  jsonb, text
) from public;
revoke all on function private.weekly_selector_reject_revision_mutation_v2() from public;
revoke all on function private.weekly_selector_validate_complete_payload_v2(
  jsonb, uuid, text, text, text, text, jsonb, integer
) from public;

revoke all on function public.get_weekly_selector_round_v2(text, text) from public;
revoke all on function public.issue_weekly_selector_token_v2(
  text, text, text, text, text, text, text, text, text, text, text, text,
  jsonb, text, text
) from public;
revoke all on function public.revoke_weekly_selector_token_v2(uuid, text) from public;
revoke all on function public.submit_weekly_selector_complete_v2(
  text, text, text, uuid, text, text, jsonb, text
) from public;
revoke all on function public.get_weekly_selector_receipt_v2(uuid, text, text) from public;
revoke all on function public.get_weekly_selector_latest_submissions_v2(
  text, text
) from public;

do $$
begin
  if exists (select 1 from pg_roles where rolname = 'anon') then
    revoke all on table public.weekly_selector_identities_v2 from anon;
    revoke all on table public.weekly_selector_tokens_v2 from anon;
    revoke all on table public.weekly_selector_submission_revisions_v2 from anon;
    revoke all on table public.weekly_selector_submissions_latest_v2 from anon;
    grant execute on function public.get_weekly_selector_round_v2(text, text) to anon;
    grant execute on function public.submit_weekly_selector_complete_v2(
      text, text, text, uuid, text, text, jsonb, text
    ) to anon;
    grant execute on function public.get_weekly_selector_receipt_v2(
      uuid, text, text
    ) to anon;
    grant execute on function public.get_weekly_selector_latest_submissions_v2(
      text, text
    ) to anon;
  end if;

  if exists (select 1 from pg_roles where rolname = 'authenticated') then
    revoke all on table public.weekly_selector_identities_v2 from authenticated;
    revoke all on table public.weekly_selector_tokens_v2 from authenticated;
    revoke all on table public.weekly_selector_submission_revisions_v2 from authenticated;
    revoke all on table public.weekly_selector_submissions_latest_v2 from authenticated;
    grant execute on function public.get_weekly_selector_round_v2(
      text, text
    ) to authenticated;
    grant execute on function public.issue_weekly_selector_token_v2(
      text, text, text, text, text, text, text, text, text, text, text, text,
      jsonb, text, text
    ) to authenticated;
    grant execute on function public.revoke_weekly_selector_token_v2(
      uuid, text
    ) to authenticated;
    grant execute on function public.submit_weekly_selector_complete_v2(
      text, text, text, uuid, text, text, jsonb, text
    ) to authenticated;
    grant execute on function public.get_weekly_selector_receipt_v2(
      uuid, text, text
    ) to authenticated;
    grant execute on function public.get_weekly_selector_latest_submissions_v2(
      text, text
    ) to authenticated;
  end if;

  if exists (select 1 from pg_roles where rolname = 'service_role') then
    grant select, insert
      on table public.weekly_selector_identities_v2 to service_role;
    grant select, insert
      on table public.weekly_selector_tokens_v2 to service_role;
    grant select, insert
      on table public.weekly_selector_submission_revisions_v2 to service_role;
    grant select, insert, update
      on table public.weekly_selector_submissions_latest_v2 to service_role;
    grant execute on function public.get_weekly_selector_round_v2(
      text, text
    ) to service_role;
    grant execute on function public.issue_weekly_selector_token_v2(
      text, text, text, text, text, text, text, text, text, text, text, text,
      jsonb, text, text
    ) to service_role;
    grant execute on function public.revoke_weekly_selector_token_v2(
      uuid, text
    ) to service_role;
    grant execute on function public.submit_weekly_selector_complete_v2(
      text, text, text, uuid, text, text, jsonb, text
    ) to service_role;
    grant execute on function public.get_weekly_selector_receipt_v2(
      uuid, text, text
    ) to service_role;
    grant execute on function public.get_weekly_selector_latest_submissions_v2(
      text, text
    ) to service_role;
  end if;
end;
$$;

comment on table public.weekly_selector_tokens_v2 is
  'Round-scoped v2 bearer tokens. Only SHA-256 hashes are persisted; raw tokens are returned once by the API.';
comment on table public.weekly_selector_submission_revisions_v2 is
  'Append-only canonical complete-batch v2 revisions accepted strictly before round close.';
comment on function public.issue_weekly_selector_token_v2(
  text, text, text, text, text, text, text, text, text, text, text, text,
  jsonb, text, text
) is
  'Issues an expiring round/environment-bound v2 token for a fully identified model configuration and strict blindness attestation.';

commit;
