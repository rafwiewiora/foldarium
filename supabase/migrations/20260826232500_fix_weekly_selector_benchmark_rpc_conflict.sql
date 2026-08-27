-- Avoid PL/pgSQL output-column ambiguity in benchmark idempotency.

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
  on conflict on constraint weekly_selector_post_close_benchmarks_v1_pkey
    do nothing;
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
