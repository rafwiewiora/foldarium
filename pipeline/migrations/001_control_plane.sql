-- Foldarium control plane: portable Postgres schema for campaign orchestration.
--
-- This migration intentionally stores object URIs, checksums, and provenance rather
-- than molecular files. It is safe to run more than once on a fresh or already
-- migrated database.

begin;

create or replace function public.foldarium_set_updated_at()
returns trigger
language plpgsql
set search_path = pg_catalog
as $$
begin
  new.updated_at = clock_timestamp();
  return new;
end;
$$;

create table if not exists public.campaigns (
  campaign_id text primary key,
  schema_version integer not null default 1 check (schema_version > 0),
  name text not null,
  source text not null,
  release_date date,
  selection_policy_version text not null,
  configuration jsonb not null default '{}'::jsonb
    check (jsonb_typeof(configuration) is not distinct from 'object'),
  status text not null default 'draft'
    check (status in ('draft', 'intake', 'predicting', 'evaluating', 'ready', 'published', 'failed', 'cancelled')),
  metadata jsonb not null default '{}'::jsonb
    check (jsonb_typeof(metadata) is not distinct from 'object'),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.targets (
  target_id text primary key,
  campaign_id text not null references public.campaigns(campaign_id) on delete cascade,
  source_id text not null,
  source_release_date date,
  selection_decision text not null default 'selected'
    check (selection_decision in ('selected', 'skipped')),
  skip_reason text,
  package_uri text,
  package_sha256 text
    check (package_sha256 is null or package_sha256 ~ '^[0-9a-f]{64}$'),
  package_schema_version integer check (package_schema_version is null or package_schema_version > 0),
  input_summary jsonb not null default '{}'::jsonb
    check (jsonb_typeof(input_summary) is not distinct from 'object'),
  metadata jsonb not null default '{}'::jsonb
    check (jsonb_typeof(metadata) is not distinct from 'object'),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (campaign_id, source_id),
  check (
    (selection_decision = 'selected' and skip_reason is null)
    or (selection_decision = 'skipped' and skip_reason is not null)
  )
);

create table if not exists public.prediction_runs (
  run_id text primary key,
  target_id text not null references public.targets(target_id) on delete cascade,
  task_payload jsonb not null check (jsonb_typeof(task_payload) is not distinct from 'object'),
  task_sha256 text not null unique check (task_sha256 ~ '^[0-9a-f]{64}$'),
  method text not null,
  method_version text not null,
  adapter_version text not null,
  method_configuration jsonb not null default '{}'::jsonb
    check (jsonb_typeof(method_configuration) is not distinct from 'object'),
  method_config_sha256 text not null
    check (method_config_sha256 ~ '^[0-9a-f]{64}$'),
  status text not null default 'pending'
    check (status in ('pending', 'queued', 'running', 'succeeded', 'failed', 'cancelled')),
  attempt_count integer not null default 0 check (attempt_count >= 0),
  max_attempts integer not null default 3 check (max_attempts > 0),
  execution_backend text,
  execution_job_id text,
  image_ref text not null,
  checkpoint_ref text,
  input_uri text not null,
  input_sha256 text not null check (input_sha256 ~ '^[0-9a-f]{64}$'),
  output_prefix text not null,
  confidence jsonb not null default '{}'::jsonb
    check (jsonb_typeof(confidence) is not distinct from 'object'),
  provenance jsonb not null default '{}'::jsonb
    check (jsonb_typeof(provenance) is not distinct from 'object'),
  result jsonb check (result is null or jsonb_typeof(result) is not distinct from 'object'),
  error_code text,
  error_message text,
  queued_at timestamptz,
  started_at timestamptz,
  completed_at timestamptz,
  lease_owner text,
  lease_expires_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  check (attempt_count <= max_attempts),
  check (
    task_payload ?& array[
      'task_id', 'target', 'method', 'method_version',
      'container_image', 'output_uri_prefix', 'config'
    ]
    and jsonb_typeof(task_payload -> 'target') is not distinct from 'object'
    and task_payload -> 'target' ? 'target_id'
    and task_payload ->> 'task_id' = run_id
    and task_payload #>> '{target,target_id}' = target_id
    and task_payload ->> 'method' = method
    and task_payload ->> 'method_version' = method_version
    and task_payload ->> 'container_image' = image_ref
    and task_payload ->> 'output_uri_prefix' = output_prefix
    and task_payload -> 'config' = method_configuration
  ),
  check (status <> 'succeeded' or completed_at is not null),
  check (status <> 'failed' or completed_at is not null)
);

create table if not exists public.prediction_artifacts (
  artifact_id text primary key,
  run_id text not null references public.prediction_runs(run_id) on delete cascade,
  sample_id text not null,
  role text not null,
  relative_path text not null,
  object_uri text not null,
  sha256 text not null check (sha256 ~ '^[0-9a-f]{64}$'),
  size_bytes bigint check (size_bytes is null or size_bytes >= 0),
  media_type text,
  metadata jsonb not null default '{}'::jsonb
    check (jsonb_typeof(metadata) is not distinct from 'object'),
  created_at timestamptz not null default now(),
  unique (run_id, sample_id, role, relative_path)
);

create table if not exists public.publication_batches (
  batch_id text primary key,
  campaign_id text not null references public.campaigns(campaign_id) on delete restrict,
  schema_version integer not null default 1 check (schema_version > 0),
  status text not null default 'draft'
    check (status in ('draft', 'validating', 'published', 'withdrawn', 'failed')),
  supersedes_batch_id text references public.publication_batches(batch_id) on delete restrict,
  manifest_uri text,
  public_manifest jsonb,
  public_manifest_sha256 text
    check (public_manifest_sha256 is null or public_manifest_sha256 ~ '^[0-9a-f]{64}$'),
  item_count integer not null default 0 check (item_count >= 0),
  validation_summary jsonb not null default '{}'::jsonb
    check (jsonb_typeof(validation_summary) is not distinct from 'object'),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  published_at timestamptz,
  withdrawn_at timestamptz,
  check (supersedes_batch_id is null or supersedes_batch_id <> batch_id),
  check (
    status <> 'published'
    or (
      public_manifest is not null
      and jsonb_typeof(public_manifest) = 'object'
      and public_manifest ? 'schema_version'
      and jsonb_typeof(public_manifest -> 'schema_version') is not distinct from 'number'
      and (public_manifest -> 'schema_version') = to_jsonb(schema_version)
      and public_manifest ? 'items'
      and jsonb_typeof(public_manifest -> 'items') is not distinct from 'array'
      and item_count = jsonb_array_length(
        case
          when jsonb_typeof(public_manifest -> 'items') = 'array'
            then public_manifest -> 'items'
          else '[]'::jsonb
        end
      )
      and public_manifest_sha256 is not null
      and published_at is not null
    )
  ),
  check (status <> 'withdrawn' or withdrawn_at is not null)
);

create index if not exists targets_campaign_idx
  on public.targets (campaign_id);

create index if not exists prediction_runs_target_status_idx
  on public.prediction_runs (target_id, status);

create index if not exists prediction_runs_queue_idx
  on public.prediction_runs (status, created_at)
  where status in ('pending', 'queued');

create index if not exists prediction_artifacts_run_idx
  on public.prediction_artifacts (run_id);

create index if not exists publication_batches_campaign_idx
  on public.publication_batches (campaign_id, created_at desc);

create unique index if not exists publication_batches_one_live_per_campaign_idx
  on public.publication_batches (campaign_id)
  where status = 'published';

drop trigger if exists campaigns_set_updated_at on public.campaigns;
create trigger campaigns_set_updated_at
before update on public.campaigns
for each row execute function public.foldarium_set_updated_at();

drop trigger if exists targets_set_updated_at on public.targets;
create trigger targets_set_updated_at
before update on public.targets
for each row execute function public.foldarium_set_updated_at();

drop trigger if exists prediction_runs_set_updated_at on public.prediction_runs;
create trigger prediction_runs_set_updated_at
before update on public.prediction_runs
for each row execute function public.foldarium_set_updated_at();

drop trigger if exists publication_batches_set_updated_at on public.publication_batches;
create trigger publication_batches_set_updated_at
before update on public.publication_batches
for each row execute function public.foldarium_set_updated_at();

-- Execution backends claim the same durable run row. A crashed worker can be
-- reclaimed only after its lease expires; switching from Modal to GCP does not
-- change the run's scientific identity.
create or replace function public.claim_prediction_run(
  p_run_id text,
  p_worker_id text,
  p_lease_seconds integer default 3600
)
returns public.prediction_runs
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_run public.prediction_runs%rowtype;
begin
  if nullif(p_worker_id, '') is null
     or p_lease_seconds is null
     or p_lease_seconds < 60
     or p_lease_seconds > 86400 then
    raise exception 'invalid worker id or lease duration'
      using errcode = '22023';
  end if;

  update public.prediction_runs
     set status = 'running',
         attempt_count = attempt_count + 1,
         execution_backend = coalesce(execution_backend, 'unspecified'),
         lease_owner = p_worker_id,
         lease_expires_at = clock_timestamp() + make_interval(secs => p_lease_seconds),
         started_at = coalesce(started_at, clock_timestamp()),
         completed_at = null,
         error_code = null,
         error_message = null
   where run_id = p_run_id
     and attempt_count < max_attempts
     and (
       status in ('pending', 'queued', 'failed')
       or (status = 'running' and lease_expires_at < clock_timestamp())
     )
   returning * into v_run;

  if not found then
    raise exception 'prediction run % is not claimable', p_run_id
      using errcode = '55P03';
  end if;
  return v_run;
end;
$$;

-- Workers upload immutable objects before calling this RPC. The RPC then records
-- every artifact and terminal run state in one database transaction. If it
-- fails, content-addressed storage objects may be safely retried/reconciled.
create or replace function public.finish_prediction_run(
  p_run_id text,
  p_worker_id text,
  p_result jsonb,
  p_artifacts jsonb default '[]'::jsonb
)
returns public.prediction_runs
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_run public.prediction_runs%rowtype;
  v_status text;
begin
  if jsonb_typeof(p_result) is distinct from 'object'
     or jsonb_typeof(p_artifacts) is distinct from 'array' then
    raise exception 'result must be an object and artifacts must be an array'
      using errcode = '22023';
  end if;
  v_status := p_result ->> 'status';
  if v_status not in ('succeeded', 'failed') then
    raise exception 'result status must be succeeded or failed'
      using errcode = '22023';
  end if;
  if v_status = 'succeeded'
     and jsonb_typeof(p_result -> 'samples') is distinct from 'array' then
    raise exception 'a succeeded result must contain a samples array'
      using errcode = '22023';
  end if;

  select *
    into v_run
    from public.prediction_runs
   where run_id = p_run_id
   for update;
  if not found then
    raise exception 'unknown prediction run: %', p_run_id
      using errcode = 'P0002';
  end if;
  if v_run.status = v_status and v_run.result = p_result then
    return v_run;
  end if;
  if v_run.status <> 'running'
     or v_run.lease_owner is distinct from p_worker_id then
    raise exception 'prediction run % is not leased by this worker', p_run_id
      using errcode = '55P03';
  end if;
  if p_result ->> 'task_id' is distinct from v_run.run_id
     or p_result ->> 'target_id' is distinct from v_run.target_id
     or p_result ->> 'method' is distinct from v_run.method
     or p_result ->> 'method_version' is distinct from v_run.method_version
     or p_result ->> 'container_image' is distinct from v_run.image_ref then
    raise exception 'prediction result identity does not match run %', p_run_id
      using errcode = '22023';
  end if;

  if exists (
    select 1
      from jsonb_to_recordset(p_artifacts) as artifact(
        artifact_id text,
        sample_id text,
        role text,
        relative_path text,
        object_uri text,
        sha256 text,
        size_bytes bigint,
        media_type text,
        metadata jsonb
      )
     where nullif(artifact_id, '') is null
        or nullif(sample_id, '') is null
        or nullif(role, '') is null
        or nullif(relative_path, '') is null
        or relative_path ~ '(^/|(^|/)\.\.(/|$))'
        or nullif(object_uri, '') is null
        or sha256 !~ '^[0-9a-f]{64}$'
        or size_bytes < 0
        or jsonb_typeof(coalesce(metadata, '{}'::jsonb)) is distinct from 'object'
  ) then
    raise exception 'invalid prediction artifact metadata'
      using errcode = '22023';
  end if;

  insert into public.prediction_artifacts (
    artifact_id, run_id, sample_id, role, relative_path, object_uri,
    sha256, size_bytes, media_type, metadata
  )
  select
    artifact_id, p_run_id, sample_id, role, relative_path, object_uri,
    sha256, size_bytes, media_type, coalesce(metadata, '{}'::jsonb)
  from jsonb_to_recordset(p_artifacts) as artifact(
    artifact_id text,
    sample_id text,
    role text,
    relative_path text,
    object_uri text,
    sha256 text,
    size_bytes bigint,
    media_type text,
    metadata jsonb
  )
  on conflict (artifact_id) do nothing;

  if exists (
    select 1
      from jsonb_to_recordset(p_artifacts) as incoming(
        artifact_id text,
        sample_id text,
        role text,
        relative_path text,
        object_uri text,
        sha256 text,
        size_bytes bigint,
        media_type text,
        metadata jsonb
      )
      join public.prediction_artifacts as stored using (artifact_id)
     where stored.run_id <> p_run_id
        or stored.sample_id <> incoming.sample_id
        or stored.role <> incoming.role
        or stored.relative_path <> incoming.relative_path
        or stored.object_uri <> incoming.object_uri
        or stored.sha256 <> incoming.sha256
  ) then
    raise exception 'artifact identity is already bound to different content'
      using errcode = '23505';
  end if;

  update public.prediction_runs
     set status = v_status,
         result = p_result,
         provenance = coalesce(p_result -> 'provenance', provenance),
         error_code = case when v_status = 'failed' then p_result ->> 'error_code' end,
         error_message = case when v_status = 'failed' then p_result ->> 'error' end,
         completed_at = clock_timestamp(),
         lease_owner = null,
         lease_expires_at = null
   where run_id = p_run_id
   returning * into v_run;
  return v_run;
end;
$$;

-- Publishing accepts only a client-safe manifest. Correctness, RMSD, and other
-- answer fields must stay in private evaluation data. The current casual quiz
-- may include anonymized method/set labels because its client groups by them.
-- Repeating a call with the same batch and digest is a no-op; changing an already
-- published batch raises an error and requires a new, superseding batch.
create or replace function public.publish_foldarium_batch(
  p_batch_id text,
  p_public_manifest jsonb,
  p_public_manifest_sha256 text,
  p_manifest_uri text default null
)
returns public.publication_batches
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_batch public.publication_batches%rowtype;
  v_superseded public.publication_batches%rowtype;
  v_item_count integer;
begin
  select *
    into v_batch
    from public.publication_batches
   where batch_id = p_batch_id
   for update;

  if not found then
    raise exception 'unknown publication batch: %', p_batch_id
      using errcode = 'P0002';
  end if;

  if v_batch.status = 'published' then
    if v_batch.public_manifest_sha256 = p_public_manifest_sha256 then
      return v_batch;
    end if;
    raise exception 'publication batch % is already published with another digest', p_batch_id
      using errcode = '23505';
  end if;

  if v_batch.status not in ('draft', 'validating') then
    raise exception 'publication batch % cannot be published from status %', p_batch_id, v_batch.status
      using errcode = '23514';
  end if;

  if p_public_manifest is null
     or jsonb_typeof(p_public_manifest) is distinct from 'object'
     or jsonb_typeof(p_public_manifest -> 'schema_version') is distinct from 'number'
     or jsonb_typeof(p_public_manifest -> 'items') is distinct from 'array' then
    raise exception 'public manifest must contain a numeric schema_version and an items array'
      using errcode = '22023';
  end if;

  if (p_public_manifest -> 'schema_version') is distinct from to_jsonb(v_batch.schema_version) then
    raise exception 'public manifest schema_version does not match publication batch'
      using errcode = '22023';
  end if;

  if p_public_manifest_sha256 is null
     or p_public_manifest_sha256 !~ '^[0-9a-f]{64}$' then
    raise exception 'public manifest digest must be a lowercase SHA-256 hex string'
      using errcode = '22023';
  end if;

  if exists (
    select 1
      from jsonb_array_elements(p_public_manifest -> 'items') as entry(item)
     where jsonb_typeof(item) is distinct from 'object'
        or nullif(item ->> 'id', '') is null
        or item ?| array['correct', 'rmsd', 'answer', 'answer_metadata', 'score']
        or jsonb_typeof(item -> 'choices') is distinct from 'array'
        or exists (
          select 1
            from jsonb_array_elements(
              case
                when jsonb_typeof(item -> 'choices') = 'array' then item -> 'choices'
                else '[]'::jsonb
              end
            ) as choice(value)
           where jsonb_typeof(value) is distinct from 'object'
              or value ?| array['correct', 'rmsd', 'answer', 'answer_metadata', 'score']
        )
  ) then
    raise exception 'public manifest items are invalid or contain reveal-only fields'
      using errcode = '22023';
  end if;

  if exists (
    select 1
      from jsonb_array_elements(p_public_manifest -> 'items') as entry(item)
     group by item ->> 'id'
    having count(*) > 1
  ) then
    raise exception 'public manifest item ids must be unique'
      using errcode = '22023';
  end if;

  v_item_count := jsonb_array_length(p_public_manifest -> 'items');

  if v_batch.supersedes_batch_id is not null then
    select *
      into v_superseded
      from public.publication_batches
     where batch_id = v_batch.supersedes_batch_id
     for update;

    if not found
       or v_superseded.campaign_id <> v_batch.campaign_id
       or v_superseded.status <> 'published' then
      raise exception 'superseded batch must be a published batch from the same campaign'
        using errcode = '23514';
    end if;

    update public.publication_batches
       set status = 'withdrawn',
           withdrawn_at = clock_timestamp()
     where batch_id = v_superseded.batch_id;
  end if;

  update public.publication_batches
     set status = 'published',
         manifest_uri = coalesce(p_manifest_uri, manifest_uri),
         public_manifest = p_public_manifest,
         public_manifest_sha256 = p_public_manifest_sha256,
         item_count = v_item_count,
         published_at = clock_timestamp(),
         withdrawn_at = null
   where batch_id = p_batch_id
   returning * into v_batch;

  update public.campaigns
     set status = 'published'
   where campaign_id = v_batch.campaign_id;

  return v_batch;
end;
$$;

-- One row per published item for PostgREST/Supabase clients. The underlying
-- control-plane tables stay private; only the pre-redacted payload is exposed.
create or replace view public.published_foldarium_items
with (security_barrier = true)
as
select
  batch.batch_id,
  batch.campaign_id,
  item.value ->> 'id' as item_id,
  batch.schema_version,
  batch.published_at,
  item.value as item
from public.publication_batches as batch
cross join lateral jsonb_array_elements(batch.public_manifest -> 'items') as item(value)
where batch.status = 'published';

alter table public.campaigns enable row level security;
alter table public.targets enable row level security;
alter table public.prediction_runs enable row level security;
alter table public.prediction_artifacts enable row level security;
alter table public.publication_batches enable row level security;

revoke all on table public.campaigns from public;
revoke all on table public.targets from public;
revoke all on table public.prediction_runs from public;
revoke all on table public.prediction_artifacts from public;
revoke all on table public.publication_batches from public;
revoke all on table public.published_foldarium_items from public;
revoke all on function public.foldarium_set_updated_at() from public;
revoke all on function public.claim_prediction_run(text, text, integer) from public;
revoke all on function public.finish_prediction_run(text, text, jsonb, jsonb) from public;
revoke all on function public.publish_foldarium_batch(text, jsonb, text, text) from public;

-- Supabase creates anon/authenticated/service_role. Conditional grants keep the
-- same migration runnable on ordinary Postgres, where deployment tooling may use
-- differently named roles.
do $$
begin
  if exists (select 1 from pg_roles where rolname = 'anon') then
    revoke all on table public.campaigns, public.targets,
      public.prediction_runs, public.prediction_artifacts,
      public.publication_batches from anon;
    revoke execute on function public.claim_prediction_run(text, text, integer) from anon;
    revoke execute on function public.finish_prediction_run(text, text, jsonb, jsonb) from anon;
    revoke execute on function public.publish_foldarium_batch(text, jsonb, text, text) from anon;
    grant select on table public.published_foldarium_items to anon;
  end if;

  if exists (select 1 from pg_roles where rolname = 'authenticated') then
    revoke all on table public.campaigns, public.targets,
      public.prediction_runs, public.prediction_artifacts,
      public.publication_batches from authenticated;
    revoke execute on function public.claim_prediction_run(text, text, integer) from authenticated;
    revoke execute on function public.finish_prediction_run(text, text, jsonb, jsonb) from authenticated;
    revoke execute on function public.publish_foldarium_batch(text, jsonb, text, text) from authenticated;
    grant select on table public.published_foldarium_items to authenticated;
  end if;

  if exists (select 1 from pg_roles where rolname = 'service_role') then
    grant select, insert, update, delete on table public.campaigns, public.targets,
      public.prediction_runs, public.prediction_artifacts,
      public.publication_batches to service_role;
    grant execute on function public.claim_prediction_run(text, text, integer)
      to service_role;
    grant execute on function public.finish_prediction_run(text, text, jsonb, jsonb)
      to service_role;
    grant execute on function public.publish_foldarium_batch(text, jsonb, text, text)
      to service_role;
  end if;
end;
$$;

comment on table public.prediction_runs is
  'Provider-neutral prediction work queue and provenance; object payloads live outside Postgres.';
comment on table public.prediction_artifacts is
  'Immutable output metadata identified by object URI and SHA-256 checksum.';
comment on column public.publication_batches.public_manifest is
  'Client-safe quiz payload only; never include correctness, RMSD, or answer metadata.';
comment on view public.published_foldarium_items is
  'Anonymous-readable, pre-redacted items from atomically published batches.';

commit;
