-- READ ONLY. Confirms the Foldarium control plane installed correctly.
select 'table' as kind, c.relname as object,
       c.relrowsecurity::text as rls_on
  from pg_class c join pg_namespace n on n.oid = c.relnamespace
 where n.nspname = 'public' and c.relkind = 'r'
   and c.relname in ('campaigns','targets','prediction_runs',
                     'prediction_artifacts','publication_batches')
union all
select 'view', table_name, 'n/a'
  from information_schema.views
 where table_schema = 'public' and table_name = 'published_foldarium_items'
union all
select 'function', routine_name, 'n/a'
  from information_schema.routines
 where routine_schema = 'public'
   and routine_name in ('claim_prediction_run','finish_prediction_run',
                        'publish_foldarium_batch','foldarium_set_updated_at')
union all
-- your existing quiz tables must be untouched, RLS still on
select 'PRE-EXISTING', c.relname, c.relrowsecurity::text
  from pg_class c join pg_namespace n on n.oid = c.relnamespace
 where n.nspname = 'public' and c.relkind = 'r'
   and c.relname in ('leaderboard_profiles','quiz_answers','quiz_sessions')
 order by kind, object;
