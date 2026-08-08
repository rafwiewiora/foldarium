-- READ ONLY. Changes nothing. Run this BEFORE the Foldarium migration.
-- 1. Do any of the migration's table names already exist?
select 'COLLISION: table' as risk, table_name
  from information_schema.tables
 where table_schema = 'public'
   and table_name in ('campaigns','targets','prediction_runs',
                      'prediction_artifacts','publication_batches')
union all
-- 2. Would create-or-replace clobber an existing function or view?
select 'COLLISION: function', routine_name
  from information_schema.routines
 where routine_schema = 'public'
   and routine_name in ('foldarium_set_updated_at','claim_prediction_run',
                        'finish_prediction_run','publish_foldarium_batch')
union all
select 'COLLISION: view', table_name
  from information_schema.views
 where table_schema = 'public'
   and table_name = 'published_foldarium_items';

-- 3. What is actually in this project right now, and is RLS already on?
select c.relname as object,
       case c.relkind when 'r' then 'table' when 'v' then 'view'
                      when 'm' then 'matview' else c.relkind::text end as kind,
       c.relrowsecurity as rls_enabled
  from pg_class c
  join pg_namespace n on n.oid = c.relnamespace
 where n.nspname = 'public' and c.relkind in ('r','v','m')
 order by kind, object;
