-- Preview rounds are iterative review artifacts. Prefer the latest eligible
-- publication there even when an operator deliberately reuses an earlier
-- voting window. Production and development remain schedule-first.

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
   order by
     case when p_environment = 'preview' then opened_at end desc nulls last,
     opens_at desc,
     opened_at desc,
     round_id desc
   limit 1
$$;

comment on function public.get_current_weekly_quiz_round(text) is
  'Returns one eligible public weekly round: latest publication for Preview, latest voting window for production and development.';
