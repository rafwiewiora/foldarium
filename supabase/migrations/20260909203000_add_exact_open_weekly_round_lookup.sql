begin;

create or replace function public.get_exact_open_weekly_quiz_round(
  p_round_id text,
  p_environment text
)
returns setof public.public_weekly_quiz_rounds
language sql
stable
security invoker
set search_path = pg_catalog, public
as $$
  select *
    from public.public_weekly_quiz_rounds
   where round_id = p_round_id
     and environment = p_environment
     and p_environment in ('production', 'preview', 'development')
     and public_status = 'open'
     and opens_at <= clock_timestamp()
     and closes_at > clock_timestamp()
   limit 1
$$;

revoke all on function public.get_exact_open_weekly_quiz_round(text, text)
  from public;

do $$
begin
  if exists (select 1 from pg_roles where rolname = 'anon') then
    grant execute on function public.get_exact_open_weekly_quiz_round(text, text)
      to anon;
  end if;
  if exists (select 1 from pg_roles where rolname = 'authenticated') then
    grant execute on function public.get_exact_open_weekly_quiz_round(text, text)
      to authenticated;
  end if;
end;
$$;

comment on function public.get_exact_open_weekly_quiz_round(text, text) is
  'Returns one exact, currently votable, unrevealed Weekly blind round for a deployment-pinned private voting client.';

commit;
