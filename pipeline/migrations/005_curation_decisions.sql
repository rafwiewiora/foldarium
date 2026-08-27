-- Private, durable records for both accepted and rejected benchmark candidates.

begin;

create table if not exists public.curation_decisions (
  decision_id text primary key
    check (decision_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'),
  source text not null
    check (source ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'),
  stage text not null
    check (stage ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'),
  target_id text not null
    check (target_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'),
  campaign_id text references public.campaigns(campaign_id) on delete restrict,
  snapshot_id text references public.prerelease_snapshots(snapshot_id) on delete restrict,
  release_week date,
  decision text not null
    check (decision ~ '^[a-z][a-z0-9-]{0,63}$'),
  reason text not null check (length(reason) between 1 and 500),
  input_sha256 text
    check (input_sha256 is null or input_sha256 ~ '^[0-9a-f]{64}$'),
  metrics jsonb not null default '{}'::jsonb check (jsonb_typeof(metrics) = 'object'),
  provenance jsonb not null default '{}'::jsonb check (jsonb_typeof(provenance) = 'object'),
  created_at timestamptz not null default now(),
  check (snapshot_id is null or campaign_id is not null)
);

create index if not exists curation_decisions_target_idx
  on public.curation_decisions (source, stage, target_id, created_at desc);
create index if not exists curation_decisions_campaign_idx
  on public.curation_decisions (campaign_id, decision, target_id)
  where campaign_id is not null;

alter table public.curation_decisions enable row level security;
revoke all on table public.curation_decisions from public;

create or replace function public.record_curation_decisions(p_decisions jsonb)
returns jsonb
language plpgsql
set search_path = pg_catalog
as $$
declare
  v_count integer;
begin
  if jsonb_typeof(p_decisions) <> 'array' or jsonb_array_length(p_decisions) < 1 then
    raise exception 'curation decisions must be a non-empty array'
      using errcode = '22023';
  end if;

  if exists (
    select 1
      from jsonb_to_recordset(p_decisions) as row(
        decision_id text, source text, stage text, target_id text,
        campaign_id text, snapshot_id text, release_week date,
        decision text, reason text, input_sha256 text,
        metrics jsonb, provenance jsonb
      )
     where decision_id is null
        or decision_id !~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'
        or source is null or source !~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'
        or stage is null or stage !~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'
        or target_id is null or target_id !~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'
        or decision is null or decision !~ '^[a-z][a-z0-9-]{0,63}$'
        or reason is null or length(reason) not between 1 and 500
        or (input_sha256 is not null and input_sha256 !~ '^[0-9a-f]{64}$')
        or coalesce(jsonb_typeof(metrics), 'object') <> 'object'
        or coalesce(jsonb_typeof(provenance), 'object') <> 'object'
        or (snapshot_id is not null and campaign_id is null)
  ) then
    raise exception 'invalid curation decision'
      using errcode = '22023';
  end if;

  if exists (
    select 1
      from jsonb_to_recordset(p_decisions) as row(
        decision_id text, source text, stage text, target_id text,
        campaign_id text, snapshot_id text, release_week date,
        decision text, reason text, input_sha256 text,
        metrics jsonb, provenance jsonb
      )
     where snapshot_id is not null
       and not exists (
         select 1
           from public.prerelease_snapshots as snapshot
          where snapshot.snapshot_id = row.snapshot_id
            and snapshot.campaign_id = row.campaign_id
       )
  ) then
    raise exception 'curation decision snapshot/campaign mismatch'
      using errcode = '23503';
  end if;

  insert into public.curation_decisions (
    decision_id, source, stage, target_id, campaign_id, snapshot_id,
    release_week, decision, reason, input_sha256, metrics, provenance
  )
  select
    decision_id, source, stage, target_id, campaign_id, snapshot_id,
    release_week, decision, reason, input_sha256,
    coalesce(metrics, '{}'::jsonb), coalesce(provenance, '{}'::jsonb)
  from jsonb_to_recordset(p_decisions) as row(
    decision_id text, source text, stage text, target_id text,
    campaign_id text, snapshot_id text, release_week date,
    decision text, reason text, input_sha256 text,
    metrics jsonb, provenance jsonb
  )
  on conflict (decision_id) do nothing;

  if exists (
    select 1
      from jsonb_to_recordset(p_decisions) as incoming(
        decision_id text, source text, stage text, target_id text,
        campaign_id text, snapshot_id text, release_week date,
        decision text, reason text, input_sha256 text,
        metrics jsonb, provenance jsonb
      )
      join public.curation_decisions as stored using (decision_id)
     where stored.source <> incoming.source
        or stored.stage <> incoming.stage
        or stored.target_id <> incoming.target_id
        or stored.campaign_id is distinct from incoming.campaign_id
        or stored.snapshot_id is distinct from incoming.snapshot_id
        or stored.release_week is distinct from incoming.release_week
        or stored.decision <> incoming.decision
        or stored.reason <> incoming.reason
        or stored.input_sha256 is distinct from incoming.input_sha256
        or stored.metrics <> coalesce(incoming.metrics, '{}'::jsonb)
        or stored.provenance <> coalesce(incoming.provenance, '{}'::jsonb)
  ) then
    raise exception 'curation decision identity is already bound to different content'
      using errcode = '23505';
  end if;

  v_count := jsonb_array_length(p_decisions);
  return jsonb_build_object('status', 'recorded', 'decision_count', v_count);
end;
$$;

create or replace function public.record_snapshot_curation_decisions()
returns trigger
language plpgsql
set search_path = pg_catalog
as $$
declare
  v_rows jsonb;
begin
  v_rows := new.metadata -> 'selection_decisions';
  if v_rows is null then
    return new;
  end if;
  if jsonb_typeof(v_rows) <> 'array' or jsonb_array_length(v_rows) < 1 then
    raise exception 'snapshot selection_decisions must be a non-empty array'
      using errcode = '22023';
  end if;

  select jsonb_agg(
    value || jsonb_build_object(
      'campaign_id', new.campaign_id,
      'snapshot_id', new.snapshot_id,
      'release_week', new.release_date
    )
  )
  into v_rows
  from jsonb_array_elements(v_rows);

  perform public.record_curation_decisions(v_rows);
  return new;
end;
$$;

drop trigger if exists prerelease_snapshots_record_curation
  on public.prerelease_snapshots;
create trigger prerelease_snapshots_record_curation
after insert on public.prerelease_snapshots
for each row execute function public.record_snapshot_curation_decisions();

revoke all on function public.record_curation_decisions(jsonb) from public;
revoke all on function public.record_snapshot_curation_decisions() from public;

do $$
begin
  if exists (select 1 from pg_roles where rolname = 'anon') then
    revoke all on table public.curation_decisions from anon;
    revoke execute on function public.record_curation_decisions(jsonb) from anon;
    revoke execute on function public.record_snapshot_curation_decisions() from anon;
  end if;
  if exists (select 1 from pg_roles where rolname = 'authenticated') then
    revoke all on table public.curation_decisions from authenticated;
    revoke execute on function public.record_curation_decisions(jsonb) from authenticated;
    revoke execute on function public.record_snapshot_curation_decisions() from authenticated;
  end if;
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    grant select, insert on table public.curation_decisions to service_role;
    grant execute on function public.record_curation_decisions(jsonb) to service_role;
  end if;
end
$$;

commit;
