-- READ ONLY. Single result set so nothing is dropped by the editor.
select 'run' as kind, run_id as id,
       method || ' | ' || status
         || ' | att ' || attempt_count || '/' || max_attempts
         || ' | ' || coalesce(execution_backend, '-')
         || ' | ' || coalesce(checkpoint_ref, '-')
         || ' | lease=' || coalesce(lease_owner, 'NULL') as detail
  from public.prediction_runs
union all
select 'campaign', campaign_id, name || ' | ' || status
  from public.campaigns
union all
select 'target', target_id, campaign_id || ' | ' || left(package_sha256, 12)
  from public.targets
 order by kind, id;
