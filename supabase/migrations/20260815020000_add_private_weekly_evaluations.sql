-- Service-role-only catalog for immutable pre-close weekly evaluation artifacts.
--
-- The result payload remains in the existing private content-addressed Storage
-- bucket.  This table records only integrity/provenance fields.  It has no
-- browser policy, public view, or public RPC.  Its insert trigger locks the
-- exact production round and rechecks every live-state binding atomically.

begin;

create schema if not exists private;
revoke all on schema private from public;

create table public.weekly_quiz_evaluations (
  evaluation_id text primary key
    check (evaluation_id ~ '^weekly_eval_[0-9a-f]{32}$'),
  round_id text not null references public.weekly_quiz_rounds(round_id),
  campaign_id text not null,
  environment text not null check (environment = 'production'),
  round_opens_at timestamptz not null,
  round_closes_at timestamptz not null,
  blind_manifest_sha256 text not null
    check (blind_manifest_sha256 ~ '^[0-9a-f]{64}$'),
  private_index_sha256 text not null
    check (private_index_sha256 ~ '^[0-9a-f]{64}$'),
  reveal_manifest_sha256 text not null
    check (reveal_manifest_sha256 ~ '^[0-9a-f]{64}$'),
  reference_set_sha256 text not null
    check (reference_set_sha256 ~ '^[0-9a-f]{64}$'),
  prediction_set_sha256 text not null
    check (prediction_set_sha256 ~ '^[0-9a-f]{64}$'),
  format_version text not null
    check (format_version = 'foldarium.weekly-private-evaluation/v1'),
  evaluator_versions jsonb not null
    check (
      case when jsonb_typeof(evaluator_versions) = 'array'
        then jsonb_array_length(evaluator_versions) > 0
        else false
      end
    ),
  reveal_policy_version text not null
    check (reveal_policy_version = 'foldarium-weekly-reveal/v1'),
  acceptance_policy_version text not null
    check (
      acceptance_policy_version = 'foldarium-weekly-cluster-any-member/v1'
    ),
  correct_rmsd_threshold_angstrom double precision not null
    check (correct_rmsd_threshold_angstrom = 1.5),
  item_count integer not null check (item_count > 0),
  choice_count integer not null check (choice_count > 0),
  artifact_object_uri text not null,
  artifact_sha256 text not null
    check (artifact_sha256 ~ '^[0-9a-f]{64}$'),
  artifact_size_bytes bigint not null check (artifact_size_bytes > 0),
  artifact_media_type text not null check (artifact_media_type = 'application/json'),
  created_at timestamptz not null default clock_timestamp(),
  unique (round_id),
  unique (round_id, blind_manifest_sha256, private_index_sha256, artifact_sha256),
  check (round_closes_at > round_opens_at),
  check (
    artifact_object_uri ~ (
      '^supabase://[A-Za-z0-9][A-Za-z0-9._-]{0,127}/sha256/'
      || substring(artifact_sha256 from 1 for 2)
      || '/'
      || artifact_sha256
      || '$'
    )
  )
);

create index weekly_quiz_evaluations_round_created_idx
  on public.weekly_quiz_evaluations (round_id, created_at desc);

create or replace function private.foldarium_validate_weekly_evaluation_catalog()
returns trigger
language plpgsql
security definer
set search_path = pg_catalog, public, private
as $$
declare
  v_round public.weekly_quiz_rounds%rowtype;
  v_private_index_sha256 text;
begin
  if tg_op <> 'INSERT' then
    raise exception 'weekly evaluation catalog rows are immutable'
      using errcode = '55000';
  end if;

  -- FOR UPDATE serializes this descriptor insert against an atomic reveal or
  -- any other change to the source row.  The trigger itself never updates it.
  select * into v_round
    from public.weekly_quiz_rounds
   where round_id = new.round_id
   for update;
  if not found then
    raise exception 'unknown weekly round: %', new.round_id using errcode = 'P0002';
  end if;

  v_private_index_sha256 := v_round.metadata #>> '{private_index,sha256}';
  if v_round.environment <> 'production'
     or v_round.status <> 'open'
     or v_round.reveal_manifest is not null
     or v_round.reveal_manifest_sha256 is not null
     or v_round.revealed_at is not null
     or clock_timestamp() < v_round.opens_at
     or clock_timestamp() >= v_round.closes_at
     or new.environment <> v_round.environment
     or new.campaign_id <> v_round.campaign_id
     or new.round_opens_at <> v_round.opens_at
     or new.round_closes_at <> v_round.closes_at
     or new.blind_manifest_sha256 <> v_round.blind_manifest_sha256
     or v_private_index_sha256 is null
     or v_private_index_sha256 !~ '^[0-9a-f]{64}$'
     or new.private_index_sha256 <> v_private_index_sha256 then
    raise exception 'weekly evaluation is not bound to one active unrevealed production round'
      using errcode = '23514';
  end if;
  return new;
end;
$$;

-- The catalog insert locks this same round row before writing its descriptor.
-- Whichever transaction wins that lock therefore validates the other side of
-- an insert/reveal race against committed state.
create or replace function private.foldarium_validate_weekly_evaluation_reveal_binding()
returns trigger
language plpgsql
security definer
set search_path = pg_catalog
as $$
declare
  v_evaluation public.weekly_quiz_evaluations%rowtype;
  v_private_index_sha256 text;
begin
  if new.status <> 'revealed' then
    return new;
  end if;

  select * into v_evaluation
    from public.weekly_quiz_evaluations
   where round_id = new.round_id;
  if not found then
    return new;
  end if;

  v_private_index_sha256 := new.metadata #>> '{private_index,sha256}';
  if new.environment is distinct from v_evaluation.environment
     or new.campaign_id is distinct from v_evaluation.campaign_id
     or new.opens_at is distinct from v_evaluation.round_opens_at
     or new.closes_at is distinct from v_evaluation.round_closes_at
     or new.blind_manifest_sha256
          is distinct from v_evaluation.blind_manifest_sha256
     or v_private_index_sha256
          is distinct from v_evaluation.private_index_sha256
     or clock_timestamp() < new.closes_at
     or new.reveal_manifest is null
     or new.reveal_manifest_sha256
          is distinct from v_evaluation.reveal_manifest_sha256
     or new.revealed_at is null then
    raise exception 'weekly reveal is not bound to its immutable evaluation catalog row'
      using errcode = '23514';
  end if;

  return new;
end;
$$;

create trigger weekly_quiz_evaluations_validate_insert
before insert on public.weekly_quiz_evaluations
for each row execute function private.foldarium_validate_weekly_evaluation_catalog();

create trigger weekly_quiz_evaluations_immutable
before update or delete on public.weekly_quiz_evaluations
for each row execute function private.foldarium_validate_weekly_evaluation_catalog();

create trigger weekly_quiz_rounds_validate_evaluation_reveal
after update of status, reveal_manifest, reveal_manifest_sha256, revealed_at
  on public.weekly_quiz_rounds
for each row execute function private.foldarium_validate_weekly_evaluation_reveal_binding();

alter table public.weekly_quiz_evaluations enable row level security;

revoke all on table public.weekly_quiz_evaluations from public;
revoke all on function private.foldarium_validate_weekly_evaluation_catalog()
  from public;
revoke all on function private.foldarium_validate_weekly_evaluation_reveal_binding()
  from public;

do $$
begin
  if exists (select 1 from pg_roles where rolname = 'anon') then
    revoke all on table public.weekly_quiz_evaluations from anon;
    revoke all on function private.foldarium_validate_weekly_evaluation_catalog()
      from anon;
    revoke all on function private.foldarium_validate_weekly_evaluation_reveal_binding()
      from anon;
  end if;
  if exists (select 1 from pg_roles where rolname = 'authenticated') then
    revoke all on table public.weekly_quiz_evaluations from authenticated;
    revoke all on function private.foldarium_validate_weekly_evaluation_catalog()
      from authenticated;
    revoke all on function private.foldarium_validate_weekly_evaluation_reveal_binding()
      from authenticated;
  end if;
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    revoke all on table public.weekly_quiz_evaluations from service_role;
    revoke all on function private.foldarium_validate_weekly_evaluation_reveal_binding()
      from service_role;
    grant select, insert on table public.weekly_quiz_evaluations to service_role;
  end if;
end;
$$;

comment on table public.weekly_quiz_evaluations is
  'Service-role-only integrity catalog for private pre-close evaluation artifacts; contains no result payload.';
comment on function private.foldarium_validate_weekly_evaluation_catalog() is
  'Atomically binds an append-only private evaluation descriptor to one active unrevealed production round.';
comment on function private.foldarium_validate_weekly_evaluation_reveal_binding() is
  'Rejects a later reveal unless its live round state and digest match an existing immutable evaluation descriptor.';

commit;
