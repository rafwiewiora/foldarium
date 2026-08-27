create table public.leaderboard_profiles (
  user_id uuid primary key references auth.users(id) on delete cascade,
  username text not null
    check (
      username = btrim(username)
      and char_length(username) between 3 and 24
      and username ~ '^[A-Za-z0-9_-]+$'
    ),
  username_key text generated always as (lower(username)) stored unique,
  updated_at timestamptz not null default now()
);

alter table public.leaderboard_profiles enable row level security;

revoke all on public.leaderboard_profiles from anon, authenticated;

create function public.claim_leaderboard_username(p_username text)
returns text
language plpgsql
security definer
set search_path = pg_catalog
as $$
declare
  claimed_user_id uuid := auth.uid();
  claimed_username text := btrim(p_username);
begin
  if claimed_user_id is null then
    raise exception 'Authentication is required to claim a leaderboard username.'
      using errcode = '42501';
  end if;

  if claimed_username is null
    or char_length(claimed_username) not between 3 and 24
    or claimed_username !~ '^[A-Za-z0-9_-]+$'
  then
    raise exception 'Username must be 3-24 letters, numbers, underscores, or hyphens.'
      using errcode = '22023';
  end if;

  insert into public.leaderboard_profiles (user_id, username)
  values (claimed_user_id, claimed_username)
  on conflict (user_id) do update
    set username = excluded.username,
        updated_at = now();

  return claimed_username;
end;
$$;

create function public.get_leaderboard()
returns table (
  username text,
  items bigint,
  sessions bigint,
  accuracy integer,
  af3_accuracy integer,
  beat_af3_by integer
)
language sql
stable
security definer
set search_path = pg_catalog
as $$
  with completed_answers as (
    select
      sessions.user_id,
      answers.session_id,
      answers.source,
      answers.item_id,
      answers.picked_correct,
      answers.af3_correct,
      row_number() over (
        partition by sessions.user_id, answers.source, answers.item_id
        order by answers.answered_at desc, answers.created_at desc, answers.id desc
      ) as recency
    from public.quiz_sessions as sessions
    join public.quiz_answers as answers
      on answers.session_id = sessions.id
    where sessions.completed_at is not null
  ),
  aggregate_scores as (
    select
      user_id,
      count(*) as items,
      round(100.0 * sum(picked_correct::integer) / count(*))::integer as accuracy,
      round(100.0 * sum(af3_correct::integer) / count(*))::integer as af3_accuracy,
      round(
        100.0 * sum(picked_correct::integer - af3_correct::integer) / count(*)
      )::integer as beat_af3_by
    from completed_answers
    where recency = 1
    group by user_id
  ),
  completed_session_counts as (
    select user_id, count(*) as sessions
    from public.quiz_sessions
    where completed_at is not null
    group by user_id
  )
  select
    profiles.username,
    scores.items,
    session_counts.sessions,
    scores.accuracy,
    scores.af3_accuracy,
    scores.beat_af3_by
  from aggregate_scores as scores
  join public.leaderboard_profiles as profiles
    on profiles.user_id = scores.user_id
  join completed_session_counts as session_counts
    on session_counts.user_id = scores.user_id
  order by scores.accuracy desc, scores.items desc, lower(profiles.username);
$$;

revoke all on function public.claim_leaderboard_username(text)
  from public, anon, authenticated;
revoke all on function public.get_leaderboard()
  from public, anon, authenticated;

grant execute on function public.claim_leaderboard_username(text)
  to authenticated;
grant execute on function public.get_leaderboard()
  to anon, authenticated;
