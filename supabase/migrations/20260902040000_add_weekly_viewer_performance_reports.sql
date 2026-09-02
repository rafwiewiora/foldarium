begin;

create table private.weekly_viewer_performance_reports (
  report_id uuid primary key,
  session_id uuid not null,
  round_id text not null,
  user_id uuid not null,
  item_id text not null check (char_length(item_id) between 1 and 200),
  question_index integer not null check (question_index >= 0),
  report jsonb not null,
  submitted_at timestamptz not null default clock_timestamp(),
  foreign key (session_id, round_id, user_id)
    references public.weekly_quiz_sessions(session_id, round_id, user_id)
    on delete cascade,
  check ((
    jsonb_typeof(report) = 'object'
    and report ->> 'schema_version' = 'foldarium.viewer-performance-diagnostics/v1'
    and report ->> 'consent' = 'explicit-beta-checkbox'
    and jsonb_typeof(report -> 'setup') = 'object'
    and jsonb_typeof(report -> 'question') = 'object'
    and jsonb_typeof(report -> 'structures') = 'object'
    and report #>> '{question,item_id}' = item_id
    and report #>> '{question,question_index}' = question_index::text
    and octet_length(report::text) <= 32768
    and report::text !~* '"(display[_-]?name|participant[_-]?name|player[_-]?name|user[_-]?agent|asset[_-]?url|ip[_-]?address|plugins?|fonts?|vendor|renderer)"[[:space:]]*:'
  ) is true)
);

create index weekly_viewer_performance_round_time_idx
  on private.weekly_viewer_performance_reports (round_id, submitted_at, report_id);
create index weekly_viewer_performance_session_time_idx
  on private.weekly_viewer_performance_reports (session_id, submitted_at, report_id);
create index weekly_viewer_performance_user_rate_idx
  on private.weekly_viewer_performance_reports (user_id, submitted_at desc);

create or replace function public.append_weekly_viewer_performance_report(
  p_report_id uuid,
  p_session_id uuid,
  p_round_id text,
  p_item_id text,
  p_question_index integer,
  p_report jsonb
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog
as $$
declare
  v_user_id uuid;
  v_session public.weekly_quiz_sessions%rowtype;
  v_existing private.weekly_viewer_performance_reports%rowtype;
  v_inserted private.weekly_viewer_performance_reports%rowtype;
begin
  v_user_id := auth.uid();
  if v_user_id is null then
    raise exception 'authentication required' using errcode = '42501';
  end if;
  if p_report_id is null or p_session_id is null
     or nullif(p_round_id, '') is null or nullif(p_item_id, '') is null
     or p_question_index is null or p_question_index < 0 then
    raise exception 'invalid viewer performance report identity' using errcode = '22023';
  end if;
  if p_report is null
     or jsonb_typeof(p_report) is distinct from 'object'
     or p_report ->> 'schema_version'
       is distinct from 'foldarium.viewer-performance-diagnostics/v1'
     or p_report ->> 'consent' is distinct from 'explicit-beta-checkbox'
     or jsonb_typeof(p_report -> 'setup') is distinct from 'object'
     or jsonb_typeof(p_report -> 'question') is distinct from 'object'
     or jsonb_typeof(p_report -> 'structures') is distinct from 'object'
     or p_report #>> '{question,item_id}' is distinct from p_item_id
     or p_report #>> '{question,question_index}' is distinct from p_question_index::text
     or octet_length(p_report::text) > 32768
     or p_report::text ~* '"(display[_-]?name|participant[_-]?name|player[_-]?name|user[_-]?agent|asset[_-]?url|ip[_-]?address|plugins?|fonts?|vendor|renderer)"[[:space:]]*:'
  then
    raise exception 'viewer performance report is invalid or contains prohibited identity fields'
      using errcode = '22023';
  end if;

  select * into v_session
    from public.weekly_quiz_sessions
   where session_id = p_session_id
   for share;
  if not found or v_session.user_id <> v_user_id or v_session.round_id <> p_round_id then
    raise exception 'unknown weekly quiz session' using errcode = 'P0002';
  end if;
  if not exists (
    select 1
      from public.weekly_quiz_rounds as round
     cross join lateral jsonb_array_elements(round.blind_manifest -> 'items')
       with ordinality as item(value, ordinal_position)
     where round.round_id = p_round_id
       and item.value ->> 'id' = p_item_id
       and item.ordinal_position - 1 = p_question_index
  ) then
    raise exception 'performance report does not reference a published item'
      using errcode = '22023';
  end if;

  perform pg_advisory_xact_lock(
    hashtextextended('weekly-viewer-performance:' || p_report_id::text, 0)
  );
  select * into v_existing
    from private.weekly_viewer_performance_reports
   where report_id = p_report_id;
  if found then
    if v_existing.session_id <> p_session_id
       or v_existing.round_id <> p_round_id
       or v_existing.user_id <> v_user_id
       or v_existing.item_id <> p_item_id
       or v_existing.question_index <> p_question_index
       or v_existing.report is distinct from p_report then
      raise exception 'performance report identity is already bound to different content'
        using errcode = '23505';
    end if;
    return jsonb_build_object(
      'report_id', v_existing.report_id,
      'session_id', v_existing.session_id,
      'round_id', v_existing.round_id,
      'submitted_at', v_existing.submitted_at,
      'idempotent', true
    );
  end if;

  perform pg_advisory_xact_lock(
    hashtextextended('weekly-viewer-performance-user:' || v_user_id::text, 0)
  );
  if (
    select count(*)
      from private.weekly_viewer_performance_reports
     where user_id = v_user_id
       and submitted_at >= clock_timestamp() - interval '1 minute'
  ) >= 20 or (
    select count(*)
      from private.weekly_viewer_performance_reports
     where user_id = v_user_id
       and submitted_at >= clock_timestamp() - interval '1 hour'
  ) >= 120 then
    raise exception 'too many viewer performance reports; try again later'
      using errcode = '42900';
  end if;

  insert into private.weekly_viewer_performance_reports (
    report_id, session_id, round_id, user_id, item_id, question_index, report
  ) values (
    p_report_id, p_session_id, p_round_id, v_user_id, p_item_id, p_question_index,
    p_report
  )
  returning * into v_inserted;

  return jsonb_build_object(
    'report_id', v_inserted.report_id,
    'session_id', v_inserted.session_id,
    'round_id', v_inserted.round_id,
    'submitted_at', v_inserted.submitted_at,
    'idempotent', false
  );
end;
$$;

revoke all on table private.weekly_viewer_performance_reports from public;
revoke all on function public.append_weekly_viewer_performance_report(
  uuid, uuid, text, text, integer, jsonb
) from public;

do $$
begin
  if exists (select 1 from pg_roles where rolname = 'anon') then
    revoke all on table private.weekly_viewer_performance_reports from anon;
    revoke all on function public.append_weekly_viewer_performance_report(
      uuid, uuid, text, text, integer, jsonb
    ) from anon;
  end if;
  if exists (select 1 from pg_roles where rolname = 'authenticated') then
    revoke all on table private.weekly_viewer_performance_reports from authenticated;
    grant execute on function public.append_weekly_viewer_performance_report(
      uuid, uuid, text, text, integer, jsonb
    ) to authenticated;
  end if;
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    grant usage on schema private to service_role;
    revoke insert, update, delete, truncate
      on table private.weekly_viewer_performance_reports from service_role;
    grant select on table private.weekly_viewer_performance_reports to service_role;
  end if;
end;
$$;

comment on table private.weekly_viewer_performance_reports is
  'Private opt-in beta viewer timing and coarse capability reports; excluded from replay views.';
comment on function public.append_weekly_viewer_performance_report(
  uuid, uuid, text, text, integer, jsonb
) is
  'Append one bounded, consented viewer performance report for the caller-owned named Weekly session.';

commit;
