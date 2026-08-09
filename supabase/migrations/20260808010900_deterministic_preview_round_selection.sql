-- Prefer the most recently published round when multiple Preview iterations
-- intentionally share the same voting window.

create or replace function public.get_current_weekly_quiz_round(p_environment text)
returns setof public.public_weekly_quiz_rounds
language sql
stable
security invoker
set search_path = pg_catalog, public
as $$
  select *
    from public.public_weekly_quiz_rounds
   where environment = p_environment
     and p_environment in ('production', 'preview', 'development')
     and opens_at <= clock_timestamp()
   order by opens_at desc, opened_at desc, round_id desc
   limit 1
$$;

comment on function public.get_current_weekly_quiz_round(text) is
  'Returns the current public weekly round for one explicit deployment environment, preferring the most recently opened round when voting windows match.';
