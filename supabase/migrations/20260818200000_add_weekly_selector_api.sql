-- Weekly selector API: private kit catalog, hashed bearer tokens, append-only submissions.

begin;

create schema if not exists extensions;
create extension if not exists pgcrypto with schema extensions;

create table if not exists private.weekly_selector_kit_catalog (
  round_id text primary key
    references public.weekly_quiz_rounds(round_id) on delete restrict,
  kit_sha256 text not null
    check (kit_sha256 ~ '^[0-9a-f]{64}$'),
  blind_manifest_sha256 text not null
    check (blind_manifest_sha256 ~ '^[0-9a-f]{64}$'),
  item_count integer not null check (item_count > 0),
  byte_size bigint not null check (byte_size > 0 and byte_size <= 536870912),
  storage_path text not null
    check (
      char_length(storage_path) between 1 and 1024
      and storage_path !~ '[[:cntrl:]]'
      and storage_path ~ '^[A-Za-z0-9][A-Za-z0-9._/-]*$'
      and position('..' in storage_path) = 0
      and position('//' in storage_path) = 0
    ),
  descriptor jsonb not null
    check (jsonb_typeof(descriptor) = 'object'),
  created_at timestamptz not null default clock_timestamp()
);

create table public.weekly_selector_identities (
  identity_id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  display_name text not null,
  method_name text not null,
  method_version text not null,
  participant_hash text not null check (participant_hash ~ '^[0-9a-f]{64}$'),
  display_name_hash text not null check (display_name_hash ~ '^[0-9a-f]{64}$'),
  created_at timestamptz not null default clock_timestamp(),
  unique (user_id, display_name, method_name, method_version),
  check (
    display_name = regexp_replace(btrim(display_name), '[[:space:]]+', ' ', 'g')
    and char_length(display_name) between 1 and 80
    and octet_length(display_name) <= 320
    and display_name !~ '[[:cntrl:]]'
  ),
  check (
    char_length(method_name) between 1 and 80
    and char_length(method_version) between 1 and 80
    and method_name !~ '[[:cntrl:]]'
    and method_version !~ '[[:cntrl:]]'
  )
);

create index weekly_selector_identities_user_created_idx
  on public.weekly_selector_identities (user_id, created_at desc);

create table public.weekly_selector_tokens (
  token_id uuid primary key default gen_random_uuid(),
  identity_id uuid not null
    references public.weekly_selector_identities(identity_id) on delete cascade,
  environment text not null
    check (environment in ('production', 'preview', 'development')),
  token_hash text not null check (token_hash ~ '^[0-9a-f]{64}$'),
  issued_at timestamptz not null default clock_timestamp(),
  unique (token_hash)
);

create index weekly_selector_tokens_identity_issued_idx
  on public.weekly_selector_tokens (identity_id, issued_at desc);

create table public.weekly_selector_submission_revisions (
  submission_id uuid not null,
  revision_number integer not null check (revision_number >= 1),
  identity_id uuid not null
    references public.weekly_selector_identities(identity_id) on delete restrict,
  round_id text not null
    references public.weekly_quiz_rounds(round_id) on delete restrict,
  payload_digest text not null check (payload_digest ~ '^[0-9a-f]{64}$'),
  payload jsonb not null,
  submitted_at timestamptz not null default clock_timestamp(),
  primary key (submission_id, revision_number),
  check (octet_length(payload::text) <= 65536),
  check ((payload ->> 'schema_version') = 'foldarium.selector-submission/v1'),
  check ((payload ->> 'submission_id') = submission_id::text)
);

create index weekly_selector_submission_revisions_identity_round_idx
  on public.weekly_selector_submission_revisions (identity_id, round_id, submitted_at desc);
create unique index weekly_selector_submission_revisions_identity_round_revision_idx
  on public.weekly_selector_submission_revisions (identity_id, round_id, revision_number);

create table public.weekly_selector_submissions_latest (
  identity_id uuid not null
    references public.weekly_selector_identities(identity_id) on delete restrict,
  round_id text not null
    references public.weekly_quiz_rounds(round_id) on delete restrict,
  submission_id uuid not null,
  revision_number integer not null check (revision_number >= 1),
  payload_digest text not null check (payload_digest ~ '^[0-9a-f]{64}$'),
  submitted_at timestamptz not null default clock_timestamp(),
  primary key (identity_id, round_id),
  unique (submission_id),
  foreign key (submission_id, revision_number)
    references public.weekly_selector_submission_revisions(submission_id, revision_number)
    on delete restrict
);

create or replace function private.weekly_selector_normalize_method(
  p_value text,
  p_label text
)
returns text
language plpgsql
immutable
set search_path = pg_catalog
as $$
declare
  v_value text;
begin
  v_value := btrim(p_value);
  if nullif(v_value, '') is null
     or char_length(v_value) > 80
     or v_value ~ '[[:cntrl:]]' then
    raise exception '% must be 1-80 characters without control characters', p_label
      using errcode = '22023';
  end if;
  return v_value;
end;
$$;

create or replace function private.weekly_selector_payload_has_forbidden_keys(
  p_value jsonb,
  p_forbidden text[]
)
returns boolean
language sql
immutable
set search_path = pg_catalog
as $$
  select exists (
    with recursive tree(key, value) as (
      select null::text, p_value
      union all
      select child.key, child.value
        from tree
        cross join lateral (
          select entry.key, entry.value
            from jsonb_each(
              case when jsonb_typeof(tree.value) = 'object' then tree.value else '{}'::jsonb end
            ) as entry(key, value)
          union all
          select null::text, entry.value
            from jsonb_array_elements(
              case when jsonb_typeof(tree.value) = 'array' then tree.value else '[]'::jsonb end
            ) as entry(value)
        ) as child(key, value)
    )
    select 1
      from tree
     where tree.key is not null
       and tree.key = any (p_forbidden)
  )
$$;

create or replace function private.weekly_selector_canonical_json(p_value jsonb)
returns text
language plpgsql
immutable
set search_path = pg_catalog, private
as $$
declare
  v_result text;
begin
  case jsonb_typeof(p_value)
    when 'object' then
      select '{' || coalesce(
        string_agg(to_jsonb(entry.key)::text || ':' || private.weekly_selector_canonical_json(entry.value), ',' order by entry.key),
        ''
      ) || '}'
        into v_result
        from jsonb_each(p_value) as entry(key, value);
    when 'array' then
      select '[' || coalesce(
        string_agg(private.weekly_selector_canonical_json(entry.value), ',' order by entry.ordinality),
        ''
      ) || ']'
        into v_result
        from jsonb_array_elements(p_value) with ordinality as entry(value, ordinality);
    else
      v_result := p_value::text;
  end case;
  return v_result;
end;
$$;

create or replace function private.weekly_selector_validate_complete_payload(
  p_payload jsonb,
  p_submission_id uuid,
  p_round_id text,
  p_kit_sha256 text,
  p_blind_manifest jsonb,
  p_expected_item_count integer
)
returns void
language plpgsql
stable
set search_path = pg_catalog
as $$
declare
  v_item jsonb;
  v_entry jsonb;
  v_item_id text;
  v_choice_id text;
  v_cluster_id text;
  v_seen_items text[] := array[]::text[];
  v_manifest_item jsonb;
  v_choice jsonb;
  v_choice_ids text[] := array[]::text[];
  v_cluster_ids text[] := array[]::text[];
  v_item_count integer := 0;
begin
  if p_payload is null
     or jsonb_typeof(p_payload) is distinct from 'object'
     or p_payload ->> 'schema_version' is distinct from 'foldarium.selector-submission/v1'
     or p_payload ->> 'submission_id' is distinct from p_submission_id::text
     or p_payload ->> 'round_id' is distinct from p_round_id
     or p_payload ->> 'kit_sha256' is distinct from p_kit_sha256
     or jsonb_typeof(p_payload -> 'items') is distinct from 'array'
     or jsonb_array_length(p_payload -> 'items') = 0
     or octet_length(p_payload::text) > 65536
     or private.weekly_selector_payload_has_forbidden_keys(
          p_payload,
          array[
            'correct', 'accepted_correct', 'rmsd', 'answer', 'answer_metadata',
            'score', 'reference', 'crystal', 'run_id', 'sample_id',
            'artifact_sha256', 'private_index', 'reveal_manifest', 'coordinates'
          ]
        ) then
    raise exception 'selector submission payload is invalid'
      using errcode = '22023';
  end if;

  if (select count(*) from jsonb_object_keys(p_payload) as key(name)) <> 5 then
    raise exception 'selector submission payload contains unknown keys'
      using errcode = '22023';
  end if;

  for v_entry in
    select value
      from jsonb_array_elements(p_payload -> 'items') as item(value)
  loop
    v_item_count := v_item_count + 1;
    if jsonb_typeof(v_entry) is distinct from 'object'
       or (select count(*) from jsonb_object_keys(v_entry) as key(name)) <> 3
       or not (v_entry ? 'item_id' and v_entry ? 'choice_id' and v_entry ? 'cluster_id') then
      raise exception 'selector submission item shape is invalid'
        using errcode = '22023';
    end if;

    v_item_id := v_entry ->> 'item_id';
    if v_item_id !~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'
       or v_item_id = any (v_seen_items) then
      raise exception 'selector submission item_id is invalid'
        using errcode = '22023';
    end if;
    v_seen_items := array_append(v_seen_items, v_item_id);

    if jsonb_typeof(v_entry -> 'choice_id') = 'null' then
      v_choice_id := null;
    elsif jsonb_typeof(v_entry -> 'choice_id') = 'string' then
      v_choice_id := v_entry ->> 'choice_id';
      if v_choice_id !~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$' then
        raise exception 'selector submission choice_id is invalid'
          using errcode = '22023';
      end if;
    else
      raise exception 'selector submission choice_id must be an ID or null'
        using errcode = '22023';
    end if;

    if jsonb_typeof(v_entry -> 'cluster_id') = 'null' then
      v_cluster_id := null;
    elsif jsonb_typeof(v_entry -> 'cluster_id') = 'string' then
      v_cluster_id := v_entry ->> 'cluster_id';
      if v_cluster_id !~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$' then
        raise exception 'selector submission cluster_id is invalid'
          using errcode = '22023';
      end if;
    else
      raise exception 'selector submission cluster_id must be an ID or null'
        using errcode = '22023';
    end if;

    select item.value
      into v_manifest_item
      from jsonb_array_elements(p_blind_manifest -> 'items') as item(value)
     where item.value ->> 'id' = v_item_id
     limit 1;

    if v_manifest_item is null then
      raise exception 'selector submission item is not in the blind manifest'
        using errcode = '22023';
    end if;

    v_choice_ids := array[]::text[];
    v_cluster_ids := array[]::text[];
    for v_choice in
      select value
        from jsonb_array_elements(v_manifest_item -> 'choices') as choice(value)
    loop
      v_choice_ids := array_append(v_choice_ids, v_choice ->> 'id');
      if v_choice ? 'cluster_id' then
        v_cluster_ids := array_append(v_cluster_ids, v_choice ->> 'cluster_id');
      end if;
    end loop;

    if v_choice_id is not null and not (v_choice_id = any (v_choice_ids)) then
      raise exception 'selector submission choice_id is not in the blind manifest'
        using errcode = '22023';
    end if;
    if v_cluster_id is not null and not (v_cluster_id = any (v_cluster_ids)) then
      raise exception 'selector submission cluster_id is not in the blind manifest'
        using errcode = '22023';
    end if;
  end loop;

  if v_item_count <> p_expected_item_count then
    raise exception 'selector submission must include every round item exactly once'
      using errcode = '22023';
  end if;
end;
$$;

create or replace function public.issue_weekly_selector_token(
  p_display_name text,
  p_method_name text,
  p_method_version text,
  p_token_hash text,
  p_environment text
)
returns table (
  token_id uuid,
  identity_id uuid,
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
  v_participant_hash text;
  v_display_name_hash text;
  v_identity_id uuid;
  v_token_id uuid;
  v_issued_at timestamptz;
begin
  v_user_id := auth.uid();
  if v_user_id is null then
    raise exception 'selector token issuance requires an authenticated user'
      using errcode = '42501';
  end if;
  if p_token_hash is null
     or p_token_hash !~ '^[0-9a-f]{64}$'
     or p_environment not in ('production', 'preview', 'development') then
    raise exception 'invalid selector token issuance request'
      using errcode = '22023';
  end if;

  v_display_name := regexp_replace(btrim(p_display_name), '[[:space:]]+', ' ', 'g');
  if nullif(v_display_name, '') is null
     or char_length(v_display_name) > 80
     or octet_length(v_display_name) > 320
     or v_display_name ~ '[[:cntrl:]]' then
    raise exception 'display name must be 1-80 characters without control characters'
      using errcode = '22023';
  end if;

  v_method_name := private.weekly_selector_normalize_method(p_method_name, 'method_name');
  v_method_version := private.weekly_selector_normalize_method(p_method_version, 'method_version');

  v_participant_hash := private.foldarium_identity_hmac('participant', v_user_id::text);
  v_display_name_hash := private.foldarium_identity_hmac(
    'display-name', v_user_id::text || ':' || lower(v_display_name)
  );

  insert into public.weekly_selector_identities (
    user_id, display_name, method_name, method_version,
    participant_hash, display_name_hash
  )
  values (
    v_user_id, v_display_name, v_method_name, v_method_version,
    v_participant_hash, v_display_name_hash
  )
  on conflict (user_id, display_name, method_name, method_version)
  do update set display_name = excluded.display_name
  returning weekly_selector_identities.identity_id into v_identity_id;

  insert into public.weekly_selector_tokens (identity_id, environment, token_hash)
  values (v_identity_id, p_environment, p_token_hash)
  returning weekly_selector_tokens.token_id, weekly_selector_tokens.issued_at
  into v_token_id, v_issued_at;

  token_id := v_token_id;
  identity_id := v_identity_id;
  issued_at := v_issued_at;
  return next;
end;
$$;

create or replace function public.submit_weekly_selector_complete(
  p_token_hash text,
  p_environment text,
  p_submission_id uuid,
  p_payload jsonb,
  p_payload_digest text
)
returns table (
  submission_id uuid,
  revision_number integer,
  round_id text,
  payload_digest text,
  submitted_at timestamptz,
  idempotent boolean
)
language plpgsql
security definer
set search_path = pg_catalog, public, private
as $$
declare
  v_identity_id uuid;
  v_round public.weekly_quiz_rounds%rowtype;
  v_kit private.weekly_selector_kit_catalog%rowtype;
  v_existing_revision public.weekly_selector_submission_revisions%rowtype;
  v_revision_number integer;
  v_computed_payload_digest text;
begin
  if p_token_hash is null
     or p_token_hash !~ '^[0-9a-f]{64}$'
     or p_environment not in ('production', 'preview', 'development')
     or p_submission_id is null
     or p_payload_digest is null
     or p_payload_digest !~ '^[0-9a-f]{64}$'
     or p_payload is null then
    raise exception 'invalid selector submission request'
      using errcode = '22023';
  end if;

  v_computed_payload_digest := encode(
    extensions.digest(
      convert_to(private.weekly_selector_canonical_json(p_payload), 'UTF8'),
      'sha256'
    ),
    'hex'
  );
  if p_payload_digest is distinct from v_computed_payload_digest then
    raise exception 'selector payload digest does not match canonical payload'
      using errcode = '22023';
  end if;

  select token.identity_id
    into v_identity_id
    from public.weekly_selector_tokens as token
   where token.token_hash = p_token_hash
     and token.environment = p_environment
   limit 1;

  if v_identity_id is null then
    raise exception 'selector bearer token is invalid'
      using errcode = '42501';
  end if;

  select *
    into v_existing_revision
    from public.weekly_selector_submission_revisions
   where submission_id = p_submission_id
   order by revision_number desc
   limit 1;

  if found then
    if v_existing_revision.identity_id = v_identity_id
       and v_existing_revision.payload_digest = p_payload_digest then
      submission_id := p_submission_id;
      revision_number := v_existing_revision.revision_number;
      round_id := v_existing_revision.round_id;
      payload_digest := v_existing_revision.payload_digest;
      submitted_at := v_existing_revision.submitted_at;
      idempotent := true;
      return next;
      return;
    end if;
    raise exception 'selector submission id is already bound to a different payload'
      using errcode = '23505';
  end if;

  select round.*
    into v_round
    from public.weekly_quiz_rounds as round
   where round.round_id = p_payload ->> 'round_id'
     and round.environment = p_environment
     and round.status = 'open'
     and clock_timestamp() >= round.opens_at
     and clock_timestamp() < round.closes_at
   limit 1;

  if not found then
    raise exception 'selector round is not open'
      using errcode = '22023';
  end if;

  select *
    into v_kit
    from private.weekly_selector_kit_catalog as kit
   where kit.round_id = v_round.round_id
   limit 1;

  if not found
     or v_kit.kit_sha256 is distinct from p_payload ->> 'kit_sha256' then
    raise exception 'selector kit binding is invalid'
      using errcode = '22023';
  end if;

  perform private.weekly_selector_validate_complete_payload(
    p_payload,
    p_submission_id,
    v_round.round_id,
    v_kit.kit_sha256,
    v_round.blind_manifest,
    v_kit.item_count
  );

  perform 1
    from public.weekly_selector_identities as identity
   where identity.identity_id = v_identity_id
   for update;

  select coalesce(max(revision.revision_number), 0) + 1
    into v_revision_number
    from public.weekly_selector_submission_revisions as revision
   where revision.identity_id = v_identity_id
     and revision.round_id = v_round.round_id;

  insert into public.weekly_selector_submission_revisions (
    submission_id, revision_number, identity_id, round_id, payload_digest, payload
  )
  values (
    p_submission_id, v_revision_number, v_identity_id, v_round.round_id,
    p_payload_digest, p_payload
  )
  returning
    weekly_selector_submission_revisions.submitted_at
  into submitted_at;

  insert into public.weekly_selector_submissions_latest (
    identity_id, round_id, submission_id, revision_number, payload_digest, submitted_at
  )
  values (
    v_identity_id, v_round.round_id, p_submission_id, v_revision_number,
    p_payload_digest, submitted_at
  )
  on conflict (identity_id, round_id)
  do update set
    submission_id = excluded.submission_id,
    revision_number = excluded.revision_number,
    payload_digest = excluded.payload_digest,
    submitted_at = excluded.submitted_at;

  submission_id := p_submission_id;
  revision_number := v_revision_number;
  round_id := v_round.round_id;
  payload_digest := p_payload_digest;
  idempotent := false;
  return next;
end;
$$;

create or replace function public.register_weekly_selector_kit(
  p_round_id text,
  p_kit_sha256 text,
  p_blind_manifest_sha256 text,
  p_item_count integer,
  p_byte_size bigint,
  p_storage_path text,
  p_descriptor jsonb
)
returns table (
  round_id text,
  kit_sha256 text,
  created_at timestamptz,
  idempotent boolean
)
language plpgsql
security definer
set search_path = pg_catalog, public, private
as $$
declare
  v_round public.weekly_quiz_rounds%rowtype;
  v_existing private.weekly_selector_kit_catalog%rowtype;
  v_created_at timestamptz;
begin
  select quiz_round.*
    into v_round
    from public.weekly_quiz_rounds as quiz_round
   where quiz_round.round_id = p_round_id
   limit 1;

  if not found
     or p_kit_sha256 !~ '^[0-9a-f]{64}$'
     or p_blind_manifest_sha256 is distinct from v_round.blind_manifest_sha256
     or p_item_count is distinct from v_round.item_count
     or p_byte_size <= 0
     or p_byte_size > 536870912
     or nullif(btrim(p_storage_path), '') is null
     or char_length(p_storage_path) > 1024
     or p_storage_path ~ '[[:cntrl:]]'
     or p_storage_path !~ '^[A-Za-z0-9][A-Za-z0-9._/-]*$'
     or position('..' in p_storage_path) > 0
     or position('//' in p_storage_path) > 0
     or jsonb_typeof(p_descriptor) is distinct from 'object'
     or p_descriptor ->> 'round_id' is distinct from p_round_id
     or p_descriptor ->> 'kit_sha256' is distinct from p_kit_sha256
     or p_descriptor ->> 'blind_manifest_sha256' is distinct from p_blind_manifest_sha256
     or (p_descriptor ->> 'item_count')::integer is distinct from p_item_count then
    raise exception 'selector kit registration is invalid'
      using errcode = '22023';
  end if;

  select *
    into v_existing
    from private.weekly_selector_kit_catalog as kit
   where kit.round_id = p_round_id
   for update;

  if found then
    if v_existing.kit_sha256 = p_kit_sha256
       and v_existing.blind_manifest_sha256 = p_blind_manifest_sha256
       and v_existing.item_count = p_item_count
       and v_existing.byte_size = p_byte_size
       and v_existing.storage_path = p_storage_path
       and v_existing.descriptor = p_descriptor then
      round_id := p_round_id;
      kit_sha256 := p_kit_sha256;
      created_at := v_existing.created_at;
      idempotent := true;
      return next;
      return;
    end if;
    raise exception 'selector kit round is already bound to different content'
      using errcode = '23505';
  end if;

  insert into private.weekly_selector_kit_catalog (
    round_id,
    kit_sha256,
    blind_manifest_sha256,
    item_count,
    byte_size,
    storage_path,
    descriptor
  )
  values (
    p_round_id,
    p_kit_sha256,
    p_blind_manifest_sha256,
    p_item_count,
    p_byte_size,
    p_storage_path,
    p_descriptor
  )
  returning weekly_selector_kit_catalog.created_at into v_created_at;

  round_id := p_round_id;
  kit_sha256 := p_kit_sha256;
  created_at := v_created_at;
  idempotent := false;
  return next;
end;
$$;

create or replace function public.get_weekly_selector_kit_descriptor(
  p_round_id text,
  p_environment text
)
returns table (
  round_id text,
  kit_sha256 text,
  blind_manifest_sha256 text,
  item_count integer,
  byte_size bigint,
  storage_path text,
  descriptor jsonb,
  created_at timestamptz
)
language sql
stable
security definer
set search_path = pg_catalog, public, private
as $$
  select
    kit.round_id,
    kit.kit_sha256,
    kit.blind_manifest_sha256,
    kit.item_count,
    kit.byte_size,
    kit.storage_path,
    kit.descriptor,
    kit.created_at
  from private.weekly_selector_kit_catalog as kit
  join public.weekly_quiz_rounds as quiz_round using (round_id)
  where kit.round_id = p_round_id
    and quiz_round.environment = p_environment
    and p_environment in ('production', 'preview', 'development')
  limit 1
$$;

create or replace function public.get_weekly_selector_latest_submissions(
  p_round_id text
)
returns table (
  round_id text,
  payload jsonb,
  display_name text,
  method_name text,
  method_version text
)
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
  select
    latest.round_id,
    revision.payload,
    identity.display_name,
    identity.method_name,
    identity.method_version
  from public.weekly_selector_submissions_latest as latest
  join public.weekly_selector_submission_revisions as revision
    on revision.submission_id = latest.submission_id
   and revision.revision_number = latest.revision_number
  join public.weekly_selector_identities as identity
    on identity.identity_id = latest.identity_id
  join public.weekly_quiz_rounds as quiz_round
    on quiz_round.round_id = latest.round_id
  where latest.round_id = p_round_id
    and quiz_round.status = 'revealed'
  order by
    lower(identity.display_name),
    lower(identity.method_name),
    lower(identity.method_version),
    identity.identity_id
$$;

create or replace function public.get_weekly_selector_receipt(
  p_submission_id uuid,
  p_token_hash text,
  p_environment text
)
returns table (
  submission_id uuid,
  revision_number integer,
  round_id text,
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
    revision.round_id,
    revision.payload_digest,
    revision.submitted_at
  from public.weekly_selector_submission_revisions as revision
  join public.weekly_selector_tokens as token
    on token.identity_id = revision.identity_id
   and token.token_hash = p_token_hash
   and token.environment = p_environment
  where revision.submission_id = p_submission_id
    and p_environment in ('production', 'preview', 'development')
  order by revision.revision_number desc
  limit 1
$$;

revoke all on schema private from public;
revoke all on table private.weekly_selector_kit_catalog from public;
revoke all on function private.weekly_selector_normalize_method(text, text) from public;
revoke all on function private.weekly_selector_payload_has_forbidden_keys(jsonb, text[]) from public;
revoke all on function private.weekly_selector_canonical_json(jsonb) from public;
revoke all on function private.weekly_selector_validate_complete_payload(
  jsonb, uuid, text, text, jsonb, integer
) from public;

alter table private.weekly_selector_kit_catalog enable row level security;
alter table public.weekly_selector_identities enable row level security;
alter table public.weekly_selector_tokens enable row level security;
alter table public.weekly_selector_submission_revisions enable row level security;
alter table public.weekly_selector_submissions_latest enable row level security;

revoke all on table public.weekly_selector_identities from public;
revoke all on table public.weekly_selector_tokens from public;
revoke all on table public.weekly_selector_submission_revisions from public;
revoke all on table public.weekly_selector_submissions_latest from public;

revoke all on function public.issue_weekly_selector_token(
  text, text, text, text, text
) from public;
revoke all on function public.submit_weekly_selector_complete(
  text, text, uuid, jsonb, text
) from public;
revoke all on function public.get_weekly_selector_kit_descriptor(text, text) from public;
revoke all on function public.register_weekly_selector_kit(
  text, text, text, integer, bigint, text, jsonb
) from public;
revoke all on function public.get_weekly_selector_latest_submissions(text) from public;
revoke all on function public.get_weekly_selector_receipt(uuid, text, text) from public;

do $$
begin
  if exists (select 1 from pg_roles where rolname = 'anon') then
    revoke all on schema private from anon;
    revoke all on table private.weekly_selector_kit_catalog from anon;
    revoke all on table public.weekly_selector_identities from anon;
    revoke all on table public.weekly_selector_tokens from anon;
    revoke all on table public.weekly_selector_submission_revisions from anon;
    revoke all on table public.weekly_selector_submissions_latest from anon;
    revoke all on function private.weekly_selector_canonical_json(jsonb) from anon;
    revoke all on function public.issue_weekly_selector_token(
      text, text, text, text, text
    ) from anon;
    revoke all on function public.get_weekly_selector_kit_descriptor(text, text) from anon;
    revoke all on function public.register_weekly_selector_kit(
      text, text, text, integer, bigint, text, jsonb
    ) from anon;
    revoke all on function public.get_weekly_selector_latest_submissions(text) from anon;
    revoke all on function public.submit_weekly_selector_complete(
      text, text, uuid, jsonb, text
    ) from anon;
    revoke all on function public.get_weekly_selector_receipt(uuid, text, text) from anon;
    grant execute on function public.get_weekly_selector_kit_descriptor(text, text) to anon;
    grant execute on function public.get_weekly_selector_latest_submissions(text) to anon;
    grant execute on function public.submit_weekly_selector_complete(
      text, text, uuid, jsonb, text
    ) to anon;
    grant execute on function public.get_weekly_selector_receipt(uuid, text, text) to anon;
  end if;

  if exists (select 1 from pg_roles where rolname = 'authenticated') then
    revoke all on schema private from authenticated;
    revoke all on table private.weekly_selector_kit_catalog from authenticated;
    revoke all on table public.weekly_selector_identities from authenticated;
    revoke all on table public.weekly_selector_tokens from authenticated;
    revoke all on table public.weekly_selector_submission_revisions from authenticated;
    revoke all on table public.weekly_selector_submissions_latest from authenticated;
    revoke all on function private.weekly_selector_canonical_json(jsonb) from authenticated;
    revoke all on function public.issue_weekly_selector_token(
      text, text, text, text, text
    ) from authenticated;
    revoke all on function public.get_weekly_selector_kit_descriptor(text, text) from authenticated;
    revoke all on function public.register_weekly_selector_kit(
      text, text, text, integer, bigint, text, jsonb
    ) from authenticated;
    revoke all on function public.get_weekly_selector_latest_submissions(text) from authenticated;
    revoke all on function public.submit_weekly_selector_complete(
      text, text, uuid, jsonb, text
    ) from authenticated;
    revoke all on function public.get_weekly_selector_receipt(uuid, text, text) from authenticated;
    grant execute on function public.issue_weekly_selector_token(
      text, text, text, text, text
    ) to authenticated;
    grant execute on function public.get_weekly_selector_kit_descriptor(text, text) to authenticated;
    grant execute on function public.get_weekly_selector_latest_submissions(text) to authenticated;
    grant execute on function public.submit_weekly_selector_complete(
      text, text, uuid, jsonb, text
    ) to authenticated;
    grant execute on function public.get_weekly_selector_receipt(
      uuid, text, text
    ) to authenticated;
  end if;

  if exists (select 1 from pg_roles where rolname = 'service_role') then
    grant select, insert on table private.weekly_selector_kit_catalog to service_role;
    grant select, insert, update, delete on table public.weekly_selector_identities to service_role;
    grant select, insert, update, delete on table public.weekly_selector_tokens to service_role;
    grant select, insert on table public.weekly_selector_submission_revisions to service_role;
    grant select, insert, update on table public.weekly_selector_submissions_latest to service_role;
    grant execute on function public.issue_weekly_selector_token(
      text, text, text, text, text
    ) to service_role;
    grant execute on function public.get_weekly_selector_kit_descriptor(text, text) to service_role;
    grant execute on function public.register_weekly_selector_kit(
      text, text, text, integer, bigint, text, jsonb
    ) to service_role;
    grant execute on function public.get_weekly_selector_latest_submissions(text) to service_role;
    grant execute on function public.submit_weekly_selector_complete(
      text, text, uuid, jsonb, text
    ) to service_role;
    grant execute on function public.get_weekly_selector_receipt(uuid, text, text) to service_role;
  end if;
end;
$$;

comment on table private.weekly_selector_kit_catalog is
  'Server-only immutable selector kit descriptors keyed by weekly round.';
comment on table public.weekly_selector_tokens is
  'Foldarium selector bearer tokens store SHA-256 hashes only; raw tokens never persist.';
comment on function public.issue_weekly_selector_token(text, text, text, text, text) is
  'Authenticated token issuance derives participant identity only from auth.uid().';
comment on table public.weekly_selector_submission_revisions is
  'Append-only complete selector submission revisions accepted before round close.';

commit;
