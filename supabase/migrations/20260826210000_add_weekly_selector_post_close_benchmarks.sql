-- Append-only, explicitly post-close model benchmarks.
--
-- These rows are not ballots and never enter the pre-close selector tables.
-- Registration is service-role only, requires a closed but unrevealed round,
-- revalidates the complete dual-mode payload, and is content-idempotent.

create table if not exists public.weekly_selector_post_close_benchmarks_v1 (
  execution_id uuid primary key,
  supersedes_execution_id uuid
    references public.weekly_selector_post_close_benchmarks_v1(execution_id)
    on delete restrict,
  environment text not null
    check (environment in ('production', 'preview', 'development')),
  round_id text not null
    references public.weekly_quiz_rounds(round_id) on delete restrict,
  run_class text not null
    check (run_class = 'post_close_benchmark'),
  display_name text not null,
  method_name text not null,
  method_version text not null,
  provider text not null,
  requested_model_id text not null,
  observed_model_ids jsonb not null,
  requested_effort text not null
    check (requested_effort in ('default', 'low', 'medium', 'high', 'max')),
  applied_effort text
    check (applied_effort in ('default', 'low', 'medium', 'high', 'max')),
  effort_reporting text not null
    check (effort_reporting in ('reported', 'not_exposed')),
  prompt_profile_id text not null
    check (prompt_profile_id = 'weekly-pose-selector-v1'),
  prompt_sha256 text not null
    check (
      prompt_sha256 =
      'e09a6d42af2538ede670dd502ae83f8b6b918e53695b3453ade5e551cfd30f85'
    ),
  input_manifest_sha256 text not null
    check (input_manifest_sha256 ~ '^[0-9a-f]{64}$'),
  tools_sha256 text not null check (tools_sha256 ~ '^[0-9a-f]{64}$'),
  config_sha256 text not null check (config_sha256 ~ '^[0-9a-f]{64}$'),
  runtime_sha256 text not null check (runtime_sha256 ~ '^[0-9a-f]{64}$'),
  blindness_attestation jsonb not null,
  blindness_attestation_sha256 text not null
    check (blindness_attestation_sha256 ~ '^[0-9a-f]{64}$'),
  output_sha256 text not null check (output_sha256 ~ '^[0-9a-f]{64}$'),
  payload jsonb not null,
  payload_digest text not null check (payload_digest ~ '^[0-9a-f]{64}$'),
  execution jsonb not null,
  execution_sha256 text not null unique
    check (execution_sha256 ~ '^[0-9a-f]{64}$'),
  started_at timestamptz not null,
  finished_at timestamptz not null,
  accepted_at timestamptz not null default clock_timestamp(),
  check (finished_at >= started_at),
  check (supersedes_execution_id is null or supersedes_execution_id <> execution_id),
  check (
    (effort_reporting = 'reported' and applied_effort is not null)
    or (effort_reporting = 'not_exposed' and applied_effort is null)
  ),
  check (
    jsonb_typeof(observed_model_ids) = 'array'
    and jsonb_array_length(observed_model_ids) = 1
  )
);

create index if not exists weekly_selector_post_close_benchmarks_round_idx
  on public.weekly_selector_post_close_benchmarks_v1
  (environment, round_id, lower(display_name), accepted_at);

create or replace function private.weekly_selector_reject_benchmark_mutation_v1()
returns trigger
language plpgsql
set search_path = pg_catalog
as $$
begin
  raise exception 'weekly selector post-close benchmarks are append-only'
    using errcode = '55000';
end;
$$;

drop trigger if exists weekly_selector_post_close_benchmarks_append_only
  on public.weekly_selector_post_close_benchmarks_v1;
create trigger weekly_selector_post_close_benchmarks_append_only
before update or delete on public.weekly_selector_post_close_benchmarks_v1
for each row execute function private.weekly_selector_reject_benchmark_mutation_v1();

create or replace function public.register_weekly_selector_benchmark_v1(
  p_execution jsonb,
  p_execution_sha256 text,
  p_payload_digest text
)
returns table (
  execution_id uuid,
  environment text,
  round_id text,
  execution_sha256 text,
  payload_digest text,
  accepted_at timestamptz,
  idempotent boolean
)
language plpgsql
security definer
set search_path = pg_catalog, public, private, extensions, auth
as $$
declare
  v_execution_id uuid;
  v_supersedes_execution_id uuid;
  v_round public.weekly_quiz_rounds%rowtype;
  v_kit private.weekly_selector_kit_catalog%rowtype;
  v_existing public.weekly_selector_post_close_benchmarks_v1%rowtype;
  v_inserted_count integer := 0;
begin
  if auth.role() is distinct from 'service_role' then
    raise exception 'selector benchmark registration requires service role'
      using errcode = '42501';
  end if;
  if jsonb_typeof(p_execution) is distinct from 'object'
     or (
       select count(*)
       from jsonb_object_keys(
         case
           when jsonb_typeof(p_execution) = 'object' then p_execution
           else '{}'::jsonb
         end
       )
     ) <> 23
     or (p_execution ->> 'schema_version')
        <> 'foldarium.selector-post-close-benchmark/v1'
     or (p_execution ->> 'run_class') <> 'post_close_benchmark'
     or (p_execution ->> 'reasoning_trace_retained') <> 'false'
     or p_execution_sha256 !~ '^[0-9a-f]{64}$'
     or p_payload_digest !~ '^[0-9a-f]{64}$'
     or p_execution_sha256 <> encode(
       extensions.digest(
         convert_to(private.weekly_selector_canonical_json(p_execution), 'UTF8'),
         'sha256'
       ),
       'hex'
     )
     or p_payload_digest <> encode(
       extensions.digest(
         convert_to(
           private.weekly_selector_canonical_json(p_execution -> 'payload'),
           'UTF8'
         ),
         'sha256'
       ),
       'hex'
     ) then
    raise exception 'invalid selector post-close benchmark envelope'
      using errcode = '22023';
  end if;

  begin
    v_execution_id := (p_execution ->> 'execution_id')::uuid;
    v_supersedes_execution_id := nullif(
      p_execution ->> 'supersedes_execution_id',
      ''
    )::uuid;
  exception when invalid_text_representation then
    raise exception 'invalid selector benchmark execution identity'
      using errcode = '22023';
  end;
  if (p_execution -> 'payload' ->> 'submission_id')::uuid <> v_execution_id then
    raise exception 'selector benchmark payload identity differs'
      using errcode = '22023';
  end if;

  select quiz_round.*
    into v_round
    from public.weekly_quiz_rounds as quiz_round
   where quiz_round.environment = p_execution ->> 'environment'
     and quiz_round.round_id = p_execution ->> 'round_id'
   for share;
  if not found
     or v_round.status <> 'open'
     or clock_timestamp() < v_round.closes_at
     or v_round.reveal_manifest is not null
     or v_round.revealed_at is not null then
    raise exception 'selector benchmark round must be closed and unrevealed'
      using errcode = '22023';
  end if;

  select kit.*
    into v_kit
    from private.weekly_selector_kit_catalog as kit
   where kit.round_id = v_round.round_id
   limit 1;
  if not found
     or v_kit.blind_manifest_sha256 <> v_round.blind_manifest_sha256
     or v_kit.kit_sha256 <> p_execution ->> 'kit_sha256'
     or p_execution ->> 'blind_manifest_sha256'
        <> v_round.blind_manifest_sha256 then
    raise exception 'selector benchmark kit binding differs'
      using errcode = '22023';
  end if;

  if p_execution -> 'provenance' ->> 'prompt_profile_id'
       <> 'weekly-pose-selector-v1'
     or p_execution -> 'provenance' ->> 'prompt_sha256'
       <> 'e09a6d42af2538ede670dd502ae83f8b6b918e53695b3453ade5e551cfd30f85'
     or not private.weekly_selector_blindness_attestation_is_valid_v2(
       p_execution -> 'blindness_attestation',
       p_execution ->> 'blindness_attestation_sha256'
     ) then
    raise exception 'selector benchmark provenance is invalid'
      using errcode = '22023';
  end if;

  perform private.weekly_selector_validate_complete_payload_v2(
    p_execution -> 'payload',
    v_execution_id,
    v_round.environment,
    v_round.round_id,
    v_round.blind_manifest_sha256,
    v_kit.kit_sha256,
    v_round.blind_manifest,
    v_round.item_count
  );

  if v_supersedes_execution_id is not null then
    perform 1
      from public.weekly_selector_post_close_benchmarks_v1 as prior
     where prior.execution_id = v_supersedes_execution_id
       and prior.environment = v_round.environment
       and prior.round_id = v_round.round_id
       and prior.display_name = p_execution ->> 'display_name'
       and prior.provider = p_execution ->> 'provider';
    if not found then
      raise exception 'selector benchmark supersession target is invalid'
        using errcode = '22023';
    end if;
  end if;

  insert into public.weekly_selector_post_close_benchmarks_v1 (
    execution_id,
    supersedes_execution_id,
    environment,
    round_id,
    run_class,
    display_name,
    method_name,
    method_version,
    provider,
    requested_model_id,
    observed_model_ids,
    requested_effort,
    applied_effort,
    effort_reporting,
    prompt_profile_id,
    prompt_sha256,
    input_manifest_sha256,
    tools_sha256,
    config_sha256,
    runtime_sha256,
    blindness_attestation,
    blindness_attestation_sha256,
    output_sha256,
    payload,
    payload_digest,
    execution,
    execution_sha256,
    started_at,
    finished_at
  )
  values (
    v_execution_id,
    v_supersedes_execution_id,
    v_round.environment,
    v_round.round_id,
    'post_close_benchmark',
    p_execution ->> 'display_name',
    p_execution ->> 'method_name',
    p_execution ->> 'method_version',
    p_execution ->> 'provider',
    p_execution -> 'model' ->> 'requested_id',
    p_execution -> 'model' -> 'observed_ids',
    p_execution -> 'model' ->> 'requested_effort',
    p_execution -> 'model' ->> 'applied_effort',
    p_execution -> 'model' ->> 'effort_reporting',
    p_execution -> 'provenance' ->> 'prompt_profile_id',
    p_execution -> 'provenance' ->> 'prompt_sha256',
    p_execution -> 'provenance' ->> 'input_manifest_sha256',
    p_execution -> 'provenance' ->> 'tools_sha256',
    p_execution -> 'provenance' ->> 'config_sha256',
    p_execution -> 'provenance' ->> 'runtime_sha256',
    p_execution -> 'blindness_attestation',
    p_execution ->> 'blindness_attestation_sha256',
    p_execution ->> 'output_sha256',
    p_execution -> 'payload',
    p_payload_digest,
    p_execution,
    p_execution_sha256,
    (p_execution ->> 'started_at')::timestamptz,
    (p_execution ->> 'finished_at')::timestamptz
  )
  on conflict (execution_id) do nothing;
  get diagnostics v_inserted_count = row_count;

  select benchmark.*
    into v_existing
    from public.weekly_selector_post_close_benchmarks_v1 as benchmark
   where benchmark.execution_id = v_execution_id;
  if not found
     or v_existing.execution_sha256 <> p_execution_sha256
     or v_existing.payload_digest <> p_payload_digest
     or v_existing.execution <> p_execution then
    raise exception 'selector benchmark execution id is already bound differently'
      using errcode = '23505';
  end if;

  execution_id := v_existing.execution_id;
  environment := v_existing.environment;
  round_id := v_existing.round_id;
  execution_sha256 := v_existing.execution_sha256;
  payload_digest := v_existing.payload_digest;
  accepted_at := v_existing.accepted_at;
  idempotent := v_inserted_count = 0;
  return next;
end;
$$;

create or replace function public.get_weekly_selector_benchmarks_v1(
  p_environment text,
  p_round_id text
)
returns table (
  run_class text,
  payload jsonb,
  display_name text,
  method_name text,
  method_version text,
  provider text,
  requested_model_id text,
  observed_model_ids jsonb,
  requested_effort text,
  applied_effort text,
  effort_reporting text,
  prompt_profile_id text,
  prompt_sha256 text,
  input_manifest_sha256 text,
  tools_sha256 text,
  config_sha256 text,
  runtime_sha256 text,
  blindness_attestation jsonb,
  blindness_attestation_sha256 text,
  execution_sha256 text
)
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
  select
    benchmark.run_class,
    benchmark.payload,
    benchmark.display_name,
    benchmark.method_name,
    benchmark.method_version,
    benchmark.provider,
    benchmark.requested_model_id,
    benchmark.observed_model_ids,
    benchmark.requested_effort,
    benchmark.applied_effort,
    benchmark.effort_reporting,
    benchmark.prompt_profile_id,
    benchmark.prompt_sha256,
    benchmark.input_manifest_sha256,
    benchmark.tools_sha256,
    benchmark.config_sha256,
    benchmark.runtime_sha256,
    benchmark.blindness_attestation,
    benchmark.blindness_attestation_sha256,
    benchmark.execution_sha256
  from public.weekly_selector_post_close_benchmarks_v1 as benchmark
  join public.weekly_quiz_rounds as quiz_round
    on quiz_round.environment = benchmark.environment
   and quiz_round.round_id = benchmark.round_id
  where benchmark.environment = p_environment
    and benchmark.round_id = p_round_id
    and quiz_round.status = 'revealed'
    and quiz_round.reveal_manifest is not null
    and quiz_round.revealed_at is not null
  order by
    lower(benchmark.display_name),
    lower(benchmark.provider),
    benchmark.execution_id
$$;

alter table public.weekly_selector_post_close_benchmarks_v1 enable row level security;
revoke all on table public.weekly_selector_post_close_benchmarks_v1 from public;
revoke all on function private.weekly_selector_reject_benchmark_mutation_v1()
  from public;
revoke all on function public.register_weekly_selector_benchmark_v1(
  jsonb, text, text
) from public;
revoke all on function public.get_weekly_selector_benchmarks_v1(text, text)
  from public;

do $$
begin
  if exists (select 1 from pg_roles where rolname = 'anon') then
    grant execute on function public.get_weekly_selector_benchmarks_v1(
      text, text
    ) to anon;
  end if;
  if exists (select 1 from pg_roles where rolname = 'authenticated') then
    grant execute on function public.get_weekly_selector_benchmarks_v1(
      text, text
    ) to authenticated;
  end if;
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    grant select, insert on table
      public.weekly_selector_post_close_benchmarks_v1 to service_role;
    grant execute on function public.register_weekly_selector_benchmark_v1(
      jsonb, text, text
    ) to service_role;
    grant execute on function public.get_weekly_selector_benchmarks_v1(
      text, text
    ) to service_role;
  end if;
end
$$;

comment on table public.weekly_selector_post_close_benchmarks_v1 is
  'Append-only blind model runs accepted only after close and before reveal; never pre-close ballots.';
comment on function public.register_weekly_selector_benchmark_v1(
  jsonb, text, text
) is
  'Service-only content-idempotent registration of a complete post-close benchmark execution.';
