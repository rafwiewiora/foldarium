begin;

create table public.weekly_quiz_trace_batches (
  trace_batch_id uuid primary key,
  session_id uuid not null,
  round_id text not null,
  user_id uuid not null,
  item_id text not null check (char_length(item_id) between 1 and 200),
  question_index integer not null check (question_index >= 0),
  visit_id uuid not null,
  first_sequence integer not null check (first_sequence >= 0),
  last_sequence integer not null check (last_sequence >= first_sequence),
  flush_reason text not null check (
    flush_reason in ('interval', 'byte_budget', 'navigation', 'vote', 'visibility', 'completion')
  ),
  trace jsonb not null,
  app_state jsonb,
  submitted_at timestamptz not null default clock_timestamp(),
  created_at timestamptz not null default clock_timestamp(),
  foreign key (session_id, round_id, user_id)
    references public.weekly_quiz_sessions(session_id, round_id, user_id)
    on delete cascade,
  unique (session_id, visit_id, first_sequence, last_sequence),
  check ((
    jsonb_typeof(trace) = 'object'
    and (trace -> 'version' = '1'::jsonb) is true
    and (trace ->> 'visit_id' = visit_id::text) is true
    and jsonb_typeof(trace -> 'entries') = 'array'
    and jsonb_array_length(trace -> 'entries') between 1 and 500
    and octet_length(trace::text) <= 491520
  ) is true),
  check (
    app_state is null
    or (
      jsonb_typeof(app_state) = 'object'
      and octet_length(app_state::text) <= 65536
    ) is true
  )
);

create index weekly_quiz_trace_batches_session_time_idx
  on public.weekly_quiz_trace_batches (session_id, submitted_at, trace_batch_id);
create index weekly_quiz_trace_batches_user_rate_idx
  on public.weekly_quiz_trace_batches (user_id, submitted_at desc);

create or replace function public.append_weekly_quiz_trace_batch(
  p_trace_batch_id uuid,
  p_session_id uuid,
  p_round_id text,
  p_item_id text,
  p_question_index integer,
  p_visit_id uuid,
  p_first_sequence integer,
  p_last_sequence integer,
  p_flush_reason text,
  p_trace jsonb,
  p_app_state jsonb default null
)
returns public.weekly_quiz_trace_batches
language plpgsql
security definer
set search_path = pg_catalog
as $$
declare
  v_user_id uuid;
  v_session public.weekly_quiz_sessions%rowtype;
  v_round public.weekly_quiz_rounds%rowtype;
  v_batch public.weekly_quiz_trace_batches%rowtype;
  v_trace_sequences integer[];
begin
  v_user_id := auth.uid();
  if v_user_id is null then
    raise exception 'authentication required' using errcode = '42501';
  end if;
  if p_trace_batch_id is null or p_session_id is null or p_visit_id is null
     or nullif(p_round_id, '') is null or nullif(p_item_id, '') is null
     or p_question_index is null or p_question_index < 0
     or p_first_sequence is null or p_first_sequence < 0
     or p_last_sequence is null or p_last_sequence < p_first_sequence
     or p_flush_reason is null or p_flush_reason not in (
       'interval', 'byte_budget', 'navigation', 'vote', 'visibility', 'completion'
     ) then
    raise exception 'invalid weekly trace batch identity' using errcode = '22023';
  end if;
  if p_trace is null
     or jsonb_typeof(p_trace) is distinct from 'object'
     or p_trace -> 'version' is distinct from '1'::jsonb
     or jsonb_typeof(p_trace -> 'entries') is distinct from 'array'
     or octet_length(p_trace::text) > 491520 then
    raise exception 'weekly trace batch is invalid or too large' using errcode = '22023';
  end if;
  if jsonb_array_length(p_trace -> 'entries') not between 1 and 500 then
    raise exception 'weekly trace batch is invalid or too large' using errcode = '22023';
  end if;
  if p_trace ->> 'visit_id' is distinct from p_visit_id::text
     or exists (
       select 1
         from jsonb_array_elements(p_trace -> 'entries') as entry(value)
        where jsonb_typeof(entry.value -> 'seq') is distinct from 'number'
           or entry.value ->> 'seq' !~ '^[0-9]+$'
     ) then
    raise exception 'weekly trace batch sequence binding is invalid' using errcode = '22023';
  end if;
  select array_agg((entry.value ->> 'seq')::integer order by entry.ordinal_position)
    into v_trace_sequences
    from jsonb_array_elements(p_trace -> 'entries')
      with ordinality as entry(value, ordinal_position);
  if v_trace_sequences[1] is distinct from p_first_sequence
     or v_trace_sequences[array_length(v_trace_sequences, 1)] is distinct from p_last_sequence
     or exists (
       select 1
         from generate_subscripts(v_trace_sequences, 1) as sequence_index
        where sequence_index > 1
          and v_trace_sequences[sequence_index] <= v_trace_sequences[sequence_index - 1]
     ) then
    raise exception 'weekly trace batch sequence binding is invalid' using errcode = '22023';
  end if;
  if p_app_state is not null
     and (
       jsonb_typeof(p_app_state) is distinct from 'object'
       or octet_length(p_app_state::text) > 65536
     ) then
    raise exception 'weekly trace batch app state is invalid or too large'
      using errcode = '22023';
  end if;

  perform pg_advisory_xact_lock(
    hashtextextended('weekly-trace-batch:' || p_trace_batch_id::text, 0)
  );
  select * into v_batch
    from public.weekly_quiz_trace_batches
   where trace_batch_id = p_trace_batch_id;
  if found then
    if v_batch.user_id <> v_user_id
       or v_batch.session_id <> p_session_id
       or v_batch.round_id <> p_round_id
       or v_batch.item_id <> p_item_id
       or v_batch.question_index <> p_question_index
       or v_batch.visit_id <> p_visit_id
       or v_batch.first_sequence <> p_first_sequence
       or v_batch.last_sequence <> p_last_sequence
       or v_batch.flush_reason <> p_flush_reason
       or v_batch.trace is distinct from p_trace
       or v_batch.app_state is distinct from p_app_state then
      raise exception 'trace batch identity is already bound to different content'
        using errcode = '23505';
    end if;
    return v_batch;
  end if;

  select * into v_session
    from public.weekly_quiz_sessions
   where session_id = p_session_id
   for share;
  if not found or v_session.user_id <> v_user_id or v_session.round_id <> p_round_id then
    raise exception 'unknown weekly quiz session' using errcode = 'P0002';
  end if;
  select * into v_round
    from public.weekly_quiz_rounds
   where round_id = p_round_id
   for share;
  if not found or not exists (
    select 1
      from jsonb_array_elements(v_round.blind_manifest -> 'items')
        with ordinality as item(value, ordinal_position)
     where item.value ->> 'id' = p_item_id
       and item.ordinal_position - 1 = p_question_index
  ) then
    raise exception 'trace batch does not reference a published item'
      using errcode = '22023';
  end if;

  perform pg_advisory_xact_lock(hashtextextended('weekly-trace:' || v_user_id::text, 0));
  if (
    select count(*)
      from public.weekly_quiz_trace_batches
     where user_id = v_user_id
       and submitted_at >= clock_timestamp() - interval '1 minute'
  ) >= 120 or (
    select count(*)
      from public.weekly_quiz_trace_batches
     where user_id = v_user_id
       and submitted_at >= clock_timestamp() - interval '1 hour'
  ) >= 3000 then
    raise exception 'too many weekly trace batches; try again later'
      using errcode = '42900';
  end if;

  insert into public.weekly_quiz_trace_batches (
    trace_batch_id, session_id, round_id, user_id, item_id, question_index,
    visit_id, first_sequence, last_sequence, flush_reason, trace, app_state
  ) values (
    p_trace_batch_id, p_session_id, p_round_id, v_user_id, p_item_id, p_question_index,
    p_visit_id, p_first_sequence, p_last_sequence, p_flush_reason, p_trace, p_app_state
  )
  returning * into v_batch;
  return v_batch;
end;
$$;

create view public.replay_weekly_trace_batches_safe
with (security_invoker = true, security_barrier = true)
as
select
  batch.trace_batch_id,
  batch.session_id,
  batch.round_id,
  session.participant_hash,
  session.display_name_hash,
  batch.item_id,
  batch.question_index,
  batch.visit_id,
  batch.first_sequence,
  batch.last_sequence,
  batch.flush_reason,
  batch.trace,
  batch.app_state,
  batch.submitted_at
from public.weekly_quiz_trace_batches as batch
join public.weekly_quiz_sessions as session
  on session.session_id = batch.session_id
 and session.round_id = batch.round_id
 and session.user_id = batch.user_id;

alter table public.weekly_quiz_trace_batches enable row level security;

revoke all on table public.weekly_quiz_trace_batches from public;
revoke all on table public.replay_weekly_trace_batches_safe from public;
revoke all on function public.append_weekly_quiz_trace_batch(
  uuid, uuid, text, text, integer, uuid, integer, integer, text, jsonb, jsonb
) from public;

do $$
begin
  if exists (select 1 from pg_roles where rolname = 'anon') then
    revoke all on table public.weekly_quiz_trace_batches from anon;
    revoke all on table public.replay_weekly_trace_batches_safe from anon;
    revoke all on function public.append_weekly_quiz_trace_batch(
      uuid, uuid, text, text, integer, uuid, integer, integer, text, jsonb, jsonb
    ) from anon;
  end if;
  if exists (select 1 from pg_roles where rolname = 'authenticated') then
    revoke all on table public.weekly_quiz_trace_batches from authenticated;
    revoke all on table public.replay_weekly_trace_batches_safe from authenticated;
    grant execute on function public.append_weekly_quiz_trace_batch(
      uuid, uuid, text, text, integer, uuid, integer, integer, text, jsonb, jsonb
    ) to authenticated;
  end if;
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    revoke insert, update, delete, truncate on table public.weekly_quiz_trace_batches
      from service_role;
    grant select on table public.weekly_quiz_trace_batches to service_role;
    grant select on table public.replay_weekly_trace_batches_safe to service_role;
  end if;
end;
$$;

comment on table public.weekly_quiz_trace_batches is
  'Append-only, idempotent interaction batches for complete weekly quiz thinking traces.';
comment on view public.replay_weekly_trace_batches_safe is
  'Server-only weekly thinking traces without auth user IDs or plaintext display names.';

commit;
