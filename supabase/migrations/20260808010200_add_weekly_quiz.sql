-- Blind Saturday-to-Wednesday quiz lifecycle. Answers stay private until reveal.

begin;

create table if not exists public.weekly_quiz_rounds (
  round_id text primary key,
  campaign_id text not null references public.campaigns(campaign_id) on delete restrict,
  status text not null default 'draft'
    check (status in ('draft', 'open', 'revealed', 'withdrawn', 'failed')),
  opens_at timestamptz not null,
  closes_at timestamptz not null,
  blind_manifest jsonb,
  blind_manifest_sha256 text
    check (blind_manifest_sha256 is null or blind_manifest_sha256 ~ '^[0-9a-f]{64}$'),
  reveal_manifest jsonb,
  reveal_manifest_sha256 text
    check (reveal_manifest_sha256 is null or reveal_manifest_sha256 ~ '^[0-9a-f]{64}$'),
  item_count integer not null default 0 check (item_count >= 0),
  opened_at timestamptz,
  revealed_at timestamptz,
  withdrawn_at timestamptz,
  metadata jsonb not null default '{}'::jsonb
    check (jsonb_typeof(metadata) is not distinct from 'object'),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  check (closes_at > opens_at),
  check (
    status = 'draft'
    or (
      blind_manifest is not null
      and jsonb_typeof(blind_manifest) = 'object'
      and blind_manifest_sha256 is not null
      and opened_at is not null
      and item_count = jsonb_array_length(blind_manifest -> 'items')
    )
  ),
  check (
    status <> 'revealed'
    or (
      reveal_manifest is not null
      and jsonb_typeof(reveal_manifest) = 'object'
      and reveal_manifest_sha256 is not null
      and revealed_at is not null
    )
  ),
  check (status <> 'withdrawn' or withdrawn_at is not null)
);

create table if not exists public.weekly_quiz_votes (
  vote_id uuid primary key,
  round_id text not null references public.weekly_quiz_rounds(round_id) on delete cascade,
  user_id uuid not null,
  item_id text not null check (length(item_id) between 1 and 200),
  choice_id text,
  picked_none boolean not null,
  submitted_at timestamptz not null default now(),
  unique (round_id, user_id, item_id),
  check (
    (picked_none and choice_id is null)
    or (not picked_none and nullif(choice_id, '') is not null)
  )
);

create index if not exists weekly_quiz_rounds_window_idx
  on public.weekly_quiz_rounds (opens_at desc, closes_at desc);
create index if not exists weekly_quiz_votes_round_item_idx
  on public.weekly_quiz_votes (round_id, item_id);

drop trigger if exists weekly_quiz_rounds_set_updated_at on public.weekly_quiz_rounds;
create trigger weekly_quiz_rounds_set_updated_at
before update on public.weekly_quiz_rounds
for each row execute function public.foldarium_set_updated_at();

create or replace function public.open_weekly_quiz_round(
  p_round_id text,
  p_campaign_id text,
  p_opens_at timestamptz,
  p_closes_at timestamptz,
  p_blind_manifest jsonb,
  p_blind_manifest_sha256 text,
  p_metadata jsonb default '{}'::jsonb
)
returns public.weekly_quiz_rounds
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_round public.weekly_quiz_rounds%rowtype;
  v_item_count integer;
begin
  if nullif(p_round_id, '') is null
     or nullif(p_campaign_id, '') is null
     or p_closes_at <= p_opens_at
     or jsonb_typeof(p_blind_manifest) is distinct from 'object'
     or (p_blind_manifest -> 'schema_version') is distinct from '1'::jsonb
     or p_blind_manifest ->> 'round_id' is distinct from p_round_id
     or jsonb_typeof(p_blind_manifest -> 'items') is distinct from 'array'
     or jsonb_array_length(p_blind_manifest -> 'items') = 0
     or p_blind_manifest_sha256 !~ '^[0-9a-f]{64}$'
     or jsonb_typeof(coalesce(p_metadata, '{}'::jsonb)) is distinct from 'object' then
    raise exception 'invalid weekly blind manifest or voting window'
      using errcode = '22023';
  end if;

  if exists (
    select 1
      from jsonb_array_elements(p_blind_manifest -> 'items') as item(value)
     where jsonb_typeof(value) is distinct from 'object'
        or nullif(value ->> 'id', '') is null
        or value ?| array['correct', 'rmsd', 'answer', 'answer_metadata', 'score', 'reference']
        or jsonb_typeof(value -> 'choices') is distinct from 'array'
        or jsonb_array_length(value -> 'choices') = 0
        or exists (
          select 1 from jsonb_array_elements(value -> 'choices') as choice(value)
           where jsonb_typeof(choice.value) is distinct from 'object'
              or nullif(choice.value ->> 'id', '') is null
              or choice.value ?| array[
                'correct', 'rmsd', 'answer', 'answer_metadata', 'score',
                'method', 'method_version', 'run_id', 'reference'
              ]
        )
  ) then
    raise exception 'blind manifest contains invalid or reveal-only fields'
      using errcode = '22023';
  end if;

  if exists (
    select 1 from jsonb_array_elements(p_blind_manifest -> 'items') as item(value)
     group by value ->> 'id' having count(*) > 1
  ) or exists (
    select 1
      from jsonb_array_elements(p_blind_manifest -> 'items') as item(value)
      cross join lateral jsonb_array_elements(item.value -> 'choices') as choice(value)
     group by item.value ->> 'id', choice.value ->> 'id' having count(*) > 1
  ) then
    raise exception 'blind manifest item and choice IDs must be unique'
      using errcode = '22023';
  end if;

  v_item_count := jsonb_array_length(p_blind_manifest -> 'items');
  insert into public.weekly_quiz_rounds (
    round_id, campaign_id, status, opens_at, closes_at,
    blind_manifest, blind_manifest_sha256, item_count, opened_at, metadata
  ) values (
    p_round_id, p_campaign_id, 'open', p_opens_at, p_closes_at,
    p_blind_manifest, p_blind_manifest_sha256, v_item_count,
    clock_timestamp(), coalesce(p_metadata, '{}'::jsonb)
  )
  on conflict (round_id) do nothing;

  select * into v_round from public.weekly_quiz_rounds where round_id = p_round_id;
  if v_round.campaign_id <> p_campaign_id
     or v_round.opens_at <> p_opens_at
     or v_round.closes_at <> p_closes_at
     or v_round.blind_manifest_sha256 <> p_blind_manifest_sha256
     or v_round.blind_manifest <> p_blind_manifest then
    raise exception 'weekly round identity is already bound to different content'
      using errcode = '23505';
  end if;
  return v_round;
end;
$$;

create or replace function public.submit_weekly_quiz_vote(
  p_vote_id uuid,
  p_round_id text,
  p_item_id text,
  p_choice_id text,
  p_picked_none boolean
)
returns public.weekly_quiz_votes
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_user_id uuid;
  v_round public.weekly_quiz_rounds%rowtype;
  v_vote public.weekly_quiz_votes%rowtype;
begin
  v_user_id := nullif(current_setting('request.jwt.claim.sub', true), '')::uuid;
  if v_user_id is null then
    raise exception 'authentication required' using errcode = '42501';
  end if;
  select * into v_round from public.weekly_quiz_rounds
   where round_id = p_round_id for share;
  if not found
     or v_round.status <> 'open'
     or clock_timestamp() < v_round.opens_at
     or clock_timestamp() >= v_round.closes_at then
    raise exception 'weekly round is not accepting votes' using errcode = '23514';
  end if;
  if nullif(p_item_id, '') is null
     or not exists (
       select 1 from jsonb_array_elements(v_round.blind_manifest -> 'items') as item(value)
        where item.value ->> 'id' = p_item_id
          and (
            (p_picked_none and p_choice_id is null)
            or (
              not p_picked_none
              and exists (
                select 1 from jsonb_array_elements(item.value -> 'choices') as choice(value)
                 where choice.value ->> 'id' = p_choice_id
              )
            )
          )
     ) then
    raise exception 'vote does not reference a published item/choice'
      using errcode = '22023';
  end if;

  insert into public.weekly_quiz_votes (
    vote_id, round_id, user_id, item_id, choice_id, picked_none, submitted_at
  ) values (
    p_vote_id, p_round_id, v_user_id, p_item_id, p_choice_id, p_picked_none,
    clock_timestamp()
  )
  on conflict (round_id, user_id, item_id) do update
     set choice_id = excluded.choice_id,
         picked_none = excluded.picked_none,
         submitted_at = excluded.submitted_at
  returning * into v_vote;
  return v_vote;
end;
$$;

create or replace function public.reveal_weekly_quiz_round(
  p_round_id text,
  p_reveal_manifest jsonb,
  p_reveal_manifest_sha256 text
)
returns public.weekly_quiz_rounds
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_round public.weekly_quiz_rounds%rowtype;
begin
  select * into v_round from public.weekly_quiz_rounds
   where round_id = p_round_id for update;
  if not found then
    raise exception 'unknown weekly round: %', p_round_id using errcode = 'P0002';
  end if;
  if v_round.status = 'revealed' then
    if v_round.reveal_manifest_sha256 = p_reveal_manifest_sha256
       and v_round.reveal_manifest = p_reveal_manifest then
      return v_round;
    end if;
    raise exception 'weekly round is already revealed with different content'
      using errcode = '23505';
  end if;
  if v_round.status <> 'open'
     or clock_timestamp() < v_round.closes_at
     or jsonb_typeof(p_reveal_manifest) is distinct from 'object'
     or (p_reveal_manifest -> 'schema_version') is distinct from '1'::jsonb
     or p_reveal_manifest ->> 'round_id' is distinct from p_round_id
     or p_reveal_manifest ->> 'blind_manifest_sha256'
          is distinct from v_round.blind_manifest_sha256
     or jsonb_typeof(p_reveal_manifest -> 'items') is distinct from 'array'
     or p_reveal_manifest_sha256 !~ '^[0-9a-f]{64}$' then
    raise exception 'weekly round cannot be revealed yet or reveal manifest is invalid'
      using errcode = '23514';
  end if;
  if exists (
    (select value ->> 'id' from jsonb_array_elements(v_round.blind_manifest -> 'items'))
    except
    (select value ->> 'id' from jsonb_array_elements(p_reveal_manifest -> 'items'))
  ) or exists (
    (select value ->> 'id' from jsonb_array_elements(p_reveal_manifest -> 'items'))
    except
    (select value ->> 'id' from jsonb_array_elements(v_round.blind_manifest -> 'items'))
  ) then
    raise exception 'reveal item IDs do not match the blind manifest'
      using errcode = '22023';
  end if;
  if exists (
    select 1
      from jsonb_array_elements(v_round.blind_manifest -> 'items') as blind(value)
      join jsonb_array_elements(p_reveal_manifest -> 'items') as reveal(value)
        on reveal.value ->> 'id' = blind.value ->> 'id'
     where jsonb_typeof(reveal.value -> 'choices') is distinct from 'array'
        or exists (
          select 1
            from jsonb_array_elements(
              case
                when jsonb_typeof(reveal.value -> 'choices') = 'array'
                  then reveal.value -> 'choices'
                else '[]'::jsonb
              end
            ) as choice(value)
           where jsonb_typeof(choice.value) is distinct from 'object'
              or nullif(choice.value ->> 'id', '') is null
              or jsonb_typeof(choice.value -> 'correct') is distinct from 'boolean'
              or jsonb_typeof(choice.value -> 'rmsd') is distinct from 'number'
              or case
                   when jsonb_typeof(choice.value -> 'rmsd') = 'number'
                     then (choice.value ->> 'rmsd')::numeric < 0
                   else false
                 end
        )
        or exists (
          select 1
            from jsonb_array_elements(
              case
                when jsonb_typeof(reveal.value -> 'choices') = 'array'
                  then reveal.value -> 'choices'
                else '[]'::jsonb
              end
            ) as choice(value)
           group by choice.value ->> 'id'
          having count(*) > 1
        )
  ) then
    raise exception 'reveal choices have an invalid shape'
      using errcode = '22023';
  end if;
  if exists (
    select 1
      from jsonb_array_elements(v_round.blind_manifest -> 'items') as blind(value)
      join jsonb_array_elements(p_reveal_manifest -> 'items') as reveal(value)
        on reveal.value ->> 'id' = blind.value ->> 'id'
     where exists (
          (select value ->> 'id' from jsonb_array_elements(blind.value -> 'choices'))
          except
          (select value ->> 'id' from jsonb_array_elements(reveal.value -> 'choices'))
        )
        or exists (
          (select value ->> 'id' from jsonb_array_elements(reveal.value -> 'choices'))
          except
          (select value ->> 'id' from jsonb_array_elements(blind.value -> 'choices'))
        )
  ) then
    raise exception 'reveal choice IDs do not exactly match the blind manifest'
      using errcode = '22023';
  end if;

  update public.weekly_quiz_rounds
     set status = 'revealed',
         reveal_manifest = p_reveal_manifest,
         reveal_manifest_sha256 = p_reveal_manifest_sha256,
         revealed_at = clock_timestamp()
   where round_id = p_round_id
   returning * into v_round;
  return v_round;
end;
$$;

create or replace view public.public_weekly_quiz_rounds
with (security_barrier = true)
as
select
  round_id, campaign_id, opens_at, closes_at, item_count,
  blind_manifest,
  case when status = 'revealed' then reveal_manifest else null end as reveal_manifest,
  case
    when status = 'revealed' then 'revealed'
    when clock_timestamp() >= closes_at then 'closed'
    when clock_timestamp() >= opens_at then 'open'
    else 'scheduled'
  end as public_status,
  opened_at, revealed_at
from public.weekly_quiz_rounds
where status in ('open', 'revealed');

create or replace view public.weekly_quiz_vote_totals
with (security_barrier = true)
as
select
  vote.round_id,
  vote.item_id,
  vote.choice_id,
  vote.picked_none,
  count(*)::bigint as vote_count
from public.weekly_quiz_votes as vote
join public.weekly_quiz_rounds as round using (round_id)
where round.status = 'revealed'
group by vote.round_id, vote.item_id, vote.choice_id, vote.picked_none;

create or replace function public.get_current_weekly_quiz_round()
returns setof public.public_weekly_quiz_rounds
language sql
stable
security invoker
set search_path = pg_catalog, public
as $$
  select *
    from public.public_weekly_quiz_rounds
   where opens_at <= clock_timestamp()
   order by opens_at desc
   limit 1
$$;

create or replace function public.get_my_weekly_quiz_votes(p_round_id text)
returns table (
  item_id text,
  choice_id text,
  picked_none boolean,
  submitted_at timestamptz
)
language sql
stable
security invoker
set search_path = pg_catalog, public
as $$
  select vote.item_id, vote.choice_id, vote.picked_none, vote.submitted_at
    from public.weekly_quiz_votes as vote
   where vote.round_id = p_round_id
   order by vote.item_id
$$;

create or replace function public.get_weekly_quiz_vote_totals(p_round_id text)
returns setof public.weekly_quiz_vote_totals
language sql
stable
security invoker
set search_path = pg_catalog, public
as $$
  select *
    from public.weekly_quiz_vote_totals
   where round_id = p_round_id
   order by item_id, picked_none, choice_id
$$;

alter table public.weekly_quiz_rounds enable row level security;
alter table public.weekly_quiz_votes enable row level security;
revoke all on table public.weekly_quiz_rounds from public;
revoke all on table public.weekly_quiz_votes from public;
revoke all on table public.public_weekly_quiz_rounds from public;
revoke all on table public.weekly_quiz_vote_totals from public;
revoke all on function public.open_weekly_quiz_round(
  text, text, timestamptz, timestamptz, jsonb, text, jsonb
) from public;
revoke all on function public.submit_weekly_quiz_vote(uuid, text, text, text, boolean)
  from public;
revoke all on function public.reveal_weekly_quiz_round(text, jsonb, text)
  from public;
revoke all on function public.get_current_weekly_quiz_round() from public;
revoke all on function public.get_my_weekly_quiz_votes(text) from public;
revoke all on function public.get_weekly_quiz_vote_totals(text) from public;

drop policy if exists "users select own weekly votes" on public.weekly_quiz_votes;

do $$
begin
  if exists (select 1 from pg_roles where rolname = 'anon') then
    grant select on table public.public_weekly_quiz_rounds,
      public.weekly_quiz_vote_totals to anon;
    grant execute on function public.get_current_weekly_quiz_round() to anon;
    grant execute on function public.get_weekly_quiz_vote_totals(text) to anon;
  end if;
  if exists (select 1 from pg_roles where rolname = 'authenticated') then
    grant select on table public.public_weekly_quiz_rounds,
      public.weekly_quiz_vote_totals to authenticated;
    grant select on table public.weekly_quiz_votes to authenticated;
    grant execute on function public.submit_weekly_quiz_vote(uuid, text, text, text, boolean)
      to authenticated;
    grant execute on function public.get_current_weekly_quiz_round() to authenticated;
    grant execute on function public.get_my_weekly_quiz_votes(text) to authenticated;
    grant execute on function public.get_weekly_quiz_vote_totals(text) to authenticated;
    execute $policy$
      create policy "users select own weekly votes"
        on public.weekly_quiz_votes for select to authenticated
        using (
          user_id = nullif(current_setting('request.jwt.claim.sub', true), '')::uuid
        )
    $policy$;
  end if;
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    grant select, insert, update, delete on table public.weekly_quiz_rounds,
      public.weekly_quiz_votes to service_role;
    grant execute on function public.open_weekly_quiz_round(
      text, text, timestamptz, timestamptz, jsonb, text, jsonb
    ) to service_role;
    grant execute on function public.reveal_weekly_quiz_round(text, jsonb, text)
      to service_role;
  end if;
end;
$$;

comment on table public.weekly_quiz_rounds is
  'Saturday blind manifests and Wednesday reveal manifests; reveal fields stay private until close.';
comment on table public.weekly_quiz_votes is
  'Authenticated blind votes containing no client-computed correctness or RMSD.';

commit;
