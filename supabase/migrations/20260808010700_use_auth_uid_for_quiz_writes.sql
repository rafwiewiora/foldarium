-- PostgREST exposes authenticated identity through auth.uid().  The legacy
-- request.jwt.claim.sub GUC is not populated by current Supabase projects.
-- Replace only the exact legacy identity expression in the already-installed
-- RPCs, preserving their validation, rate limits, idempotency, and grants.
do $$
declare
  function_signature regprocedure;
  definition text;
  repaired_definition text;
begin
  foreach function_signature in array array[
    'public.submit_weekly_quiz_vote(uuid,text,text,text,boolean)'::regprocedure,
    'public.start_named_quiz_session(uuid,text,text,text)'::regprocedure,
    'public.start_named_weekly_quiz_session(uuid,text,text,jsonb)'::regprocedure,
    'public.complete_named_weekly_quiz_session(uuid)'::regprocedure,
    'public.submit_weekly_quiz_vote_attempt(uuid,uuid,text,text,integer,text,boolean,jsonb,jsonb,text)'::regprocedure,
    'public.submit_user_suggestion(uuid,text,text,uuid,uuid,text,text,jsonb,jsonb,jsonb)'::regprocedure
  ] loop
    select pg_get_functiondef(function_signature::oid) into definition;
    repaired_definition := replace(
      definition,
      'nullif(current_setting(''request.jwt.claim.sub'', true), '''')::uuid',
      'auth.uid()'
    );
    if repaired_definition = definition then
      raise exception 'expected legacy JWT identity expression in %', function_signature;
    end if;
    execute repaired_definition;
  end loop;
end;
$$;

drop policy if exists "users select own weekly votes"
  on public.weekly_quiz_votes;
create policy "users select own weekly votes"
  on public.weekly_quiz_votes for select to authenticated
  using (user_id = auth.uid());

