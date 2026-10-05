-- Read-only diagnostics, never terminal-call evidence or retry authorization.
begin;
alter function public.weekly_automation_snapshot_v1() set schema private;
alter function private.weekly_automation_snapshot_v1() rename to weekly_automation_snapshot_before_lease_diagnostics_v1;
revoke all on function private.weekly_automation_snapshot_before_lease_diagnostics_v1() from public,anon,authenticated,service_role;

create function public.weekly_automation_snapshot_v1()
returns jsonb language plpgsql security definer
set search_path=pg_catalog,public,private,auth as $$
declare result jsonb; expired_count bigint; diagnostics jsonb; observed_at timestamptz:=clock_timestamp();
begin
 if auth.role() is distinct from 'service_role' then raise exception 'service role required' using errcode='42501'; end if;
 result:=private.weekly_automation_snapshot_before_lease_diagnostics_v1();
 -- Count and bounded details share one statement snapshot, even while other
 -- service workers finish actions concurrently.
 with expired as materialized (
  select action_key,action->>'kind' as kind,action->>'identity' as identity,attempt_count,lease_until
  from private.weekly_automation_actions where status='running' and lease_until<=observed_at
 )
 select (select count(*) from expired),coalesce((
  select jsonb_agg(jsonb_build_object(
   'action_key',a.action_key,'kind',a.kind,'identity',a.identity,
   'attempt_count',a.attempt_count,'lease_until',a.lease_until,
   'diagnostic','expired-lease-unresolved') order by a.lease_until,a.action_key)
  from (select * from expired order by lease_until,action_key limit 100) a
 ),'[]'::jsonb) into expired_count,diagnostics;
 return result||jsonb_build_object('expired_running_actions',diagnostics,
  'expired_running_actions_count',expired_count,
  'expired_running_actions_truncated',expired_count>100,
  'lease_diagnostics_observed_at',observed_at);
end $$;
revoke all on function public.weekly_automation_snapshot_v1() from public,anon,authenticated;
grant execute on function public.weekly_automation_snapshot_v1() to service_role;
commit;
