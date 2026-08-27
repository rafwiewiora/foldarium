create table public.quiz_sessions (
  id uuid primary key,
  user_id uuid not null default auth.uid() references auth.users(id) on delete cascade,
  source text not null check (source in ('cameo', 'rnp')),
  difficulty text not null check (difficulty in ('easy', 'hard')),
  started_at timestamptz not null,
  completed_at timestamptz,
  created_at timestamptz not null default now(),
  check (completed_at is null or completed_at >= started_at)
);

create table public.quiz_answers (
  id uuid primary key,
  session_id uuid not null references public.quiz_sessions(id) on delete cascade,
  question_index integer not null check (question_index >= 0),
  item_id text not null check (length(item_id) between 1 and 200),
  source text not null check (source in ('cameo', 'rnp')),
  difficulty text not null check (difficulty in ('easy', 'hard')),
  picked_none boolean not null,
  picked_sample integer,
  picked_correct boolean not null,
  picked_rmsd double precision check (picked_rmsd is null or picked_rmsd >= 0),
  af3_pick_sample integer,
  af3_correct boolean not null,
  has_correct boolean not null,
  n_clusters integer not null check (n_clusters > 0),
  answered_at timestamptz not null,
  created_at timestamptz not null default now(),
  unique (session_id, question_index),
  check (
    (picked_none and picked_sample is null and picked_rmsd is null)
    or (not picked_none and picked_sample is not null)
  )
);

create index quiz_sessions_user_started_idx
  on public.quiz_sessions (user_id, started_at desc);

alter table public.quiz_sessions enable row level security;
alter table public.quiz_answers enable row level security;

revoke all on public.quiz_sessions from anon, authenticated;
revoke all on public.quiz_answers from anon, authenticated;
grant select, insert on public.quiz_sessions to authenticated;
grant update (completed_at) on public.quiz_sessions to authenticated;
grant select, insert on public.quiz_answers to authenticated;

create policy "users select own sessions"
  on public.quiz_sessions for select to authenticated
  using (user_id = auth.uid());
create policy "users insert own sessions"
  on public.quiz_sessions for insert to authenticated
  with check (user_id = auth.uid());
create policy "users complete own sessions"
  on public.quiz_sessions for update to authenticated
  using (user_id = auth.uid())
  with check (user_id = auth.uid());

create policy "users select answers from own sessions"
  on public.quiz_answers for select to authenticated
  using (exists (
    select 1 from public.quiz_sessions
    where quiz_sessions.id = quiz_answers.session_id
      and quiz_sessions.user_id = auth.uid()
  ));
create policy "users insert answers into own sessions"
  on public.quiz_answers for insert to authenticated
  with check (exists (
    select 1 from public.quiz_sessions
    where quiz_sessions.id = quiz_answers.session_id
      and quiz_sessions.user_id = auth.uid()
  ));
