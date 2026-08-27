-- READ ONLY. Post-run verification: runs, artifacts, and storage identity.
select 'run' as kind, run_id as id,
       method || ' | ' || status
         || ' | att ' || attempt_count || '/' || max_attempts
         || ' | ' || coalesce(to_char(completed_at,'HH24:MI:SS'),'-')
         || ' | lease=' || coalesce(lease_owner,'NULL')
         || ' | err=' || coalesce(error_code,'-') as detail
  from public.prediction_runs
union all
select 'artifact', a.run_id || ' / ' || a.sample_id || ' / ' || a.role,
       a.relative_path || ' | ' || a.size_bytes || ' B | ' || left(a.sha256,12)
         || ' | ' || a.object_uri
  from public.prediction_artifacts a
union all
select 'result-samples', run_id,
       coalesce(jsonb_array_length(result -> 'samples')::text, 'none')
         || ' sample(s) | ' || coalesce((result ->> 'duration_seconds'), '-') || ' s'
  from public.prediction_runs
 where result is not null
 order by kind, id;
