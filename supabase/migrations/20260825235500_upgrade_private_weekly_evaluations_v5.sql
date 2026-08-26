-- Upgrade the private weekly evaluation catalog for deterministic v5
-- retrospectives generated after voting closes. The catalog remains
-- service-role-only and accepts either side of the atomic reveal transition.

begin;

alter table public.weekly_quiz_evaluations
  drop constraint if exists weekly_quiz_evaluations_format_version_check;

alter table public.weekly_quiz_evaluations
  add constraint weekly_quiz_evaluations_format_version_check
  check (format_version = 'foldarium.weekly-private-evaluation/v5');

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

  select * into v_round
    from public.weekly_quiz_rounds
   where round_id = new.round_id
   for update;
  if not found then
    raise exception 'unknown weekly round: %', new.round_id using errcode = 'P0002';
  end if;

  v_private_index_sha256 := v_round.metadata #>> '{private_index,sha256}';
  if v_round.environment <> 'production'
     or v_round.status not in ('open', 'revealed')
     or clock_timestamp() < v_round.closes_at
     or new.environment <> v_round.environment
     or new.campaign_id <> v_round.campaign_id
     or new.round_opens_at <> v_round.opens_at
     or new.round_closes_at <> v_round.closes_at
     or new.blind_manifest_sha256 <> v_round.blind_manifest_sha256
     or v_private_index_sha256 is null
     or v_private_index_sha256 !~ '^[0-9a-f]{64}$'
     or new.private_index_sha256 <> v_private_index_sha256 then
    raise exception 'weekly evaluation is not bound to one closed production round'
      using errcode = '23514';
  end if;

  if v_round.status = 'open' and (
       v_round.reveal_manifest is not null
       or v_round.reveal_manifest_sha256 is not null
       or v_round.revealed_at is not null
     ) then
    raise exception 'open weekly evaluation source has inconsistent reveal state'
      using errcode = '23514';
  end if;

  if v_round.status = 'revealed' and (
       v_round.reveal_manifest is null
       or v_round.reveal_manifest_sha256 is null
       or v_round.revealed_at is null
       or new.reveal_manifest_sha256 <> v_round.reveal_manifest_sha256
     ) then
    raise exception 'revealed weekly evaluation source is not digest-bound'
      using errcode = '23514';
  end if;

  return new;
end;
$$;

comment on table public.weekly_quiz_evaluations is
  'Service-role-only integrity catalog for immutable post-close weekly retrospective artifacts; contains no result payload.';
comment on function private.foldarium_validate_weekly_evaluation_catalog() is
  'Atomically binds an append-only private evaluation descriptor to one closed production round before or after reveal.';

commit;
