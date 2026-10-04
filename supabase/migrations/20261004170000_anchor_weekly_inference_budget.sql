-- Budget initialization authority lives outside the inference Volume. A lost
-- acknowledgement consumes the grant: retries must preserve the existing ledger.
begin;

create table private.weekly_automation_inference_starts (
  execution_id uuid primary key references private.weekly_automation_benchmarks(execution_id),
  round_id text not null references private.weekly_automation_round_policies(round_id),
  environment text not null,
  blind_manifest_sha256 text not null,
  kit_sha256 text not null check (kit_sha256 ~ '^[0-9a-f]{64}$'),
  driver text not null,
  model_id text not null,
  config_sha256 text not null,
  max_cost_usd numeric not null check (max_cost_usd > 0 and max_cost_usd::text not in ('NaN','Infinity','-Infinity')),
  granted_at timestamptz not null default clock_timestamp()
);

create function private.prevent_weekly_inference_start_mutation()
returns trigger language plpgsql set search_path = pg_catalog as $$
begin
  raise exception 'inference initialization authority is immutable' using errcode='23514';
end $$;
create trigger weekly_inference_start_immutable
before update or delete on private.weekly_automation_inference_starts
for each row execute function private.prevent_weekly_inference_start_mutation();

create function public.claim_weekly_automation_inference_start_v1(
  p_execution_id uuid, p_config_sha256 text, p_kit_sha256 text
)
returns jsonb language plpgsql security definer
set search_path = pg_catalog, public, private, auth as $$
declare
  r public.weekly_quiz_rounds%rowtype;
  b private.weekly_automation_benchmarks%rowtype;
  p private.weekly_automation_round_policies%rowtype;
  existing private.weekly_automation_inference_starts%rowtype;
  first_claim boolean := false;
begin
  if auth.role() is distinct from 'service_role' then raise exception 'service role required' using errcode='42501'; end if;
  -- Lock in the same round-first order as release. Only the first caller may
  -- initialize a budget; concurrent callers can never obtain a second grant.
  select r0.* into strict r from public.weekly_quiz_rounds r0
    join private.weekly_automation_benchmarks b0 on b0.round_id=r0.round_id
    where b0.execution_id=p_execution_id for update of r0;
  select * into strict b from private.weekly_automation_benchmarks where execution_id=p_execution_id for update;
  select * into strict p from private.weekly_automation_round_policies where round_id=r.round_id;
  if r.status is distinct from 'open' or r.revealed_at is not null
    or b.driver is distinct from 'anthropic-api' or b.max_cost_usd is null
    or b.artifact_uri is not null
    or b.config_sha256 is distinct from p_config_sha256
    or p.environment is distinct from r.environment
    or p.blind_manifest_sha256 is distinct from r.blind_manifest_sha256
    or not (p_execution_id = any(p.expected_execution_ids))
    or not exists(select 1 from private.weekly_selector_kit_catalog k
      where k.round_id=r.round_id and k.blind_manifest_sha256=r.blind_manifest_sha256
      and k.kit_sha256=p_kit_sha256) then
    raise exception 'inference initialization source or frozen expectation mismatch';
  end if;
  select * into existing from private.weekly_automation_inference_starts where execution_id=p_execution_id;
  if found then
    if existing.round_id is distinct from r.round_id or existing.environment is distinct from r.environment
      or existing.blind_manifest_sha256 is distinct from r.blind_manifest_sha256
      or existing.kit_sha256 is distinct from p_kit_sha256 or existing.driver is distinct from b.driver
      or existing.model_id is distinct from b.model_id or existing.config_sha256 is distinct from b.config_sha256
      or existing.max_cost_usd is distinct from b.max_cost_usd then
      raise exception 'inference initialization authority is bound differently';
    end if;
  else
    insert into private.weekly_automation_inference_starts(
      execution_id,round_id,environment,blind_manifest_sha256,kit_sha256,driver,model_id,config_sha256,max_cost_usd
    ) values (b.execution_id,r.round_id,r.environment,r.blind_manifest_sha256,p_kit_sha256,b.driver,b.model_id,b.config_sha256,b.max_cost_usd)
    returning * into existing;
    first_claim := true;
  end if;
  return (to_jsonb(existing)-'granted_at') || jsonb_build_object('first_claim',first_claim);
end $$;

alter table private.weekly_automation_inference_starts enable row level security;
revoke all on table private.weekly_automation_inference_starts from public,anon,authenticated,service_role;
revoke all on function private.prevent_weekly_inference_start_mutation() from public,anon,authenticated,service_role;
revoke all on function public.claim_weekly_automation_inference_start_v1(uuid,text,text) from public,anon,authenticated;
grant execute on function public.claim_weekly_automation_inference_start_v1(uuid,text,text) to service_role;
commit;
