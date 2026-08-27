-- Atomic registration for replayable Saturday prerelease snapshots and tasks.

begin;

create table if not exists public.prerelease_snapshots (
  snapshot_id text primary key,
  campaign_id text not null references public.campaigns(campaign_id) on delete restrict,
  release_date date not null,
  plan_sha256 text not null unique check (plan_sha256 ~ '^[0-9a-f]{64}$'),
  files jsonb not null check (jsonb_typeof(files) is not distinct from 'object'),
  metadata jsonb not null default '{}'::jsonb
    check (jsonb_typeof(metadata) is not distinct from 'object'),
  created_at timestamptz not null default now()
);

create index if not exists prerelease_snapshots_campaign_idx
  on public.prerelease_snapshots (campaign_id, created_at desc);

create or replace function public.register_weekly_prediction_plan(
  p_snapshot jsonb,
  p_campaign jsonb,
  p_targets jsonb,
  p_runs jsonb
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_campaign_id text;
  v_snapshot_id text;
  v_target_count integer;
  v_run_count integer;
begin
  if jsonb_typeof(p_snapshot) is distinct from 'object'
     or jsonb_typeof(p_campaign) is distinct from 'object'
     or jsonb_typeof(p_targets) is distinct from 'array'
     or jsonb_typeof(p_runs) is distinct from 'array'
     or jsonb_array_length(p_targets) = 0
     or jsonb_array_length(p_runs) = 0 then
    raise exception 'weekly plan requires snapshot/campaign objects and non-empty target/run arrays'
      using errcode = '22023';
  end if;

  v_campaign_id := p_campaign ->> 'campaign_id';
  v_snapshot_id := p_snapshot ->> 'snapshot_id';
  if nullif(v_campaign_id, '') is null
     or nullif(v_snapshot_id, '') is null
     or p_snapshot ->> 'campaign_id' is distinct from v_campaign_id
     or p_snapshot ->> 'plan_sha256' !~ '^[0-9a-f]{64}$'
     or jsonb_typeof(p_snapshot -> 'files') is distinct from 'object'
     or jsonb_typeof(coalesce(p_snapshot -> 'metadata', '{}'::jsonb)) is distinct from 'object'
     or jsonb_typeof(coalesce(p_campaign -> 'configuration', '{}'::jsonb)) is distinct from 'object'
     or jsonb_typeof(coalesce(p_campaign -> 'metadata', '{}'::jsonb)) is distinct from 'object' then
    raise exception 'invalid weekly snapshot or campaign identity'
      using errcode = '22023';
  end if;

  if exists (
    select 1
      from jsonb_to_recordset(p_targets) as target(
        target_id text, campaign_id text, source_id text,
        source_release_date date, package_uri text, package_sha256 text,
        package_schema_version integer, input_summary jsonb, metadata jsonb
      )
     where nullif(target_id, '') is null
        or campaign_id is distinct from v_campaign_id
        or nullif(source_id, '') is null
        or nullif(package_uri, '') is null
        or package_sha256 !~ '^[0-9a-f]{64}$'
        or package_schema_version < 1
        or jsonb_typeof(input_summary) is distinct from 'object'
        or jsonb_typeof(coalesce(metadata, '{}'::jsonb)) is distinct from 'object'
  ) then
    raise exception 'invalid target row in weekly plan'
      using errcode = '22023';
  end if;

  if exists (
    select 1
      from jsonb_to_recordset(p_runs) as run(
        run_id text, target_id text, task_payload jsonb, task_sha256 text,
        method text, method_version text, adapter_version text,
        method_configuration jsonb, method_config_sha256 text, status text,
        max_attempts integer, execution_backend text, image_ref text,
        checkpoint_ref text, input_uri text, input_sha256 text, output_prefix text
      )
     where nullif(run_id, '') is null
        or nullif(target_id, '') is null
        or jsonb_typeof(task_payload) is distinct from 'object'
        or task_sha256 !~ '^[0-9a-f]{64}$'
        or method_config_sha256 !~ '^[0-9a-f]{64}$'
        or input_sha256 !~ '^[0-9a-f]{64}$'
        or status not in ('pending', 'queued')
        or max_attempts < 1
        or not exists (
          select 1
            from jsonb_to_recordset(p_targets) as target(target_id text)
           where target.target_id = run.target_id
        )
  ) then
    raise exception 'invalid prediction run row in weekly plan'
      using errcode = '22023';
  end if;

  insert into public.campaigns (
    campaign_id, name, source, release_date, selection_policy_version,
    configuration, status, metadata
  )
  select
    campaign_id, name, source, release_date, selection_policy_version,
    coalesce(configuration, '{}'::jsonb), status, coalesce(metadata, '{}'::jsonb)
  from jsonb_to_record(p_campaign) as campaign(
    campaign_id text, name text, source text, release_date date,
    selection_policy_version text, configuration jsonb, status text, metadata jsonb
  )
  on conflict (campaign_id) do nothing;

  if exists (
    select 1 from public.campaigns
     where campaign_id = v_campaign_id
       and (
         name <> p_campaign ->> 'name'
         or source <> p_campaign ->> 'source'
         or release_date is distinct from (p_campaign ->> 'release_date')::date
         or selection_policy_version <> p_campaign ->> 'selection_policy_version'
         or configuration <> coalesce(p_campaign -> 'configuration', '{}'::jsonb)
       )
  ) then
    raise exception 'campaign identity is already bound to different content'
      using errcode = '23505';
  end if;

  insert into public.prerelease_snapshots (
    snapshot_id, campaign_id, release_date, plan_sha256, files, metadata
  )
  select
    snapshot_id, campaign_id, release_date, plan_sha256, files,
    coalesce(metadata, '{}'::jsonb)
  from jsonb_to_record(p_snapshot) as snapshot(
    snapshot_id text, campaign_id text, release_date date,
    plan_sha256 text, files jsonb, metadata jsonb
  )
  on conflict (snapshot_id) do nothing;

  if exists (
    select 1 from public.prerelease_snapshots
     where snapshot_id = v_snapshot_id
       and (
         campaign_id <> v_campaign_id
         or plan_sha256 <> p_snapshot ->> 'plan_sha256'
         or files <> p_snapshot -> 'files'
       )
  ) then
    raise exception 'snapshot identity is already bound to different content'
      using errcode = '23505';
  end if;

  insert into public.targets (
    target_id, campaign_id, source_id, source_release_date,
    package_uri, package_sha256, package_schema_version,
    input_summary, metadata
  )
  select
    target_id, campaign_id, source_id, source_release_date,
    package_uri, package_sha256, package_schema_version,
    input_summary, coalesce(metadata, '{}'::jsonb)
  from jsonb_to_recordset(p_targets) as target(
    target_id text, campaign_id text, source_id text,
    source_release_date date, package_uri text, package_sha256 text,
    package_schema_version integer, input_summary jsonb, metadata jsonb
  )
  on conflict (target_id) do nothing;

  if exists (
    select 1
      from jsonb_to_recordset(p_targets) as incoming(
        target_id text, campaign_id text, source_id text,
        source_release_date date, package_uri text, package_sha256 text,
        package_schema_version integer, input_summary jsonb, metadata jsonb
      )
      join public.targets as stored using (target_id)
     where stored.campaign_id <> incoming.campaign_id
        or stored.source_id <> incoming.source_id
        or stored.package_uri <> incoming.package_uri
        or stored.package_sha256 <> incoming.package_sha256
  ) then
    raise exception 'target identity is already bound to different content'
      using errcode = '23505';
  end if;

  insert into public.prediction_runs (
    run_id, target_id, task_payload, task_sha256, method, method_version,
    adapter_version, method_configuration, method_config_sha256, status,
    max_attempts, execution_backend, image_ref, checkpoint_ref,
    input_uri, input_sha256, output_prefix
  )
  select
    run_id, target_id, task_payload, task_sha256, method, method_version,
    adapter_version, method_configuration, method_config_sha256, status,
    max_attempts, execution_backend, image_ref, checkpoint_ref,
    input_uri, input_sha256, output_prefix
  from jsonb_to_recordset(p_runs) as run(
    run_id text, target_id text, task_payload jsonb, task_sha256 text,
    method text, method_version text, adapter_version text,
    method_configuration jsonb, method_config_sha256 text, status text,
    max_attempts integer, execution_backend text, image_ref text,
    checkpoint_ref text, input_uri text, input_sha256 text, output_prefix text
  )
  on conflict (run_id) do nothing;

  if exists (
    select 1
      from jsonb_to_recordset(p_runs) as incoming(
        run_id text, task_sha256 text, input_uri text, input_sha256 text
      )
      join public.prediction_runs as stored using (run_id)
     where stored.task_sha256 <> incoming.task_sha256
        or stored.input_uri <> incoming.input_uri
        or stored.input_sha256 <> incoming.input_sha256
  ) then
    raise exception 'prediction run identity is already bound to different content'
      using errcode = '23505';
  end if;

  v_target_count := jsonb_array_length(p_targets);
  v_run_count := jsonb_array_length(p_runs);
  return jsonb_build_object(
    'status', 'registered',
    'snapshot_id', v_snapshot_id,
    'campaign_id', v_campaign_id,
    'target_count', v_target_count,
    'run_count', v_run_count
  );
end;
$$;

alter table public.prerelease_snapshots enable row level security;
revoke all on table public.prerelease_snapshots from public;
revoke all on function public.register_weekly_prediction_plan(jsonb, jsonb, jsonb, jsonb)
  from public;

do $$
begin
  if exists (select 1 from pg_roles where rolname = 'anon') then
    revoke all on table public.prerelease_snapshots from anon;
    revoke execute on function public.register_weekly_prediction_plan(jsonb, jsonb, jsonb, jsonb)
      from anon;
  end if;
  if exists (select 1 from pg_roles where rolname = 'authenticated') then
    revoke all on table public.prerelease_snapshots from authenticated;
    revoke execute on function public.register_weekly_prediction_plan(jsonb, jsonb, jsonb, jsonb)
      from authenticated;
  end if;
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    grant select, insert on table public.prerelease_snapshots to service_role;
    grant execute on function public.register_weekly_prediction_plan(jsonb, jsonb, jsonb, jsonb)
      to service_role;
  end if;
end;
$$;

comment on table public.prerelease_snapshots is
  'Immutable, content-addressed Saturday wwPDB/CAMEO source snapshots for replay.';

commit;
