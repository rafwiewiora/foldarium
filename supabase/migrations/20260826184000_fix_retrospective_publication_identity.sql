begin;

-- Match pipeline stable_id(): hash the complete canonical JSON object. The
-- original SQL omitted the final JSON string quote before the closing brace.
create or replace function public.register_weekly_retrospective_publication(
  p_publication jsonb,
  p_source_snapshot_canonical text
)
returns public.weekly_retrospective_publications
language plpgsql
security definer
set search_path = pg_catalog, public, private, extensions
as $$
declare
  v_expected_source jsonb;
  v_source jsonb;
  v_expected_publication_id text;
  v_publication public.weekly_retrospective_publications%rowtype;
begin
  if jsonb_typeof(p_publication) is distinct from 'object'
     or nullif(p_source_snapshot_canonical, '') is null then
    raise exception 'retrospective publication input is invalid'
      using errcode = '22023';
  end if;

  begin
    v_source := p_source_snapshot_canonical::jsonb;
  exception when others then
    raise exception 'retrospective source snapshot is not valid JSON'
      using errcode = '22023';
  end;

  lock table public.weekly_quiz_votes in share mode;
  lock table public.weekly_quiz_vote_attempts in share mode;
  lock table public.weekly_quiz_sessions in share mode;
  lock table public.weekly_retrospective_automated_identities in share mode;

  v_expected_source := private.foldarium_expected_weekly_retrospective_source(
    p_publication ->> 'round_id'
  );
  if v_expected_source is null or v_source is distinct from v_expected_source then
    raise exception 'retrospective source snapshot differs from final votes or sessions'
      using errcode = '23514';
  end if;
  if encode(
       extensions.digest(convert_to(p_source_snapshot_canonical, 'UTF8'), 'sha256'),
       'hex'
     ) is distinct from p_publication ->> 'source_snapshot_sha256' then
    raise exception 'retrospective source snapshot digest is inconsistent'
      using errcode = '23514';
  end if;
  if octet_length(convert_to(p_source_snapshot_canonical, 'UTF8'))
       is distinct from (p_publication ->> 'source_snapshot_size_bytes')::bigint then
    raise exception 'retrospective source snapshot size is inconsistent'
      using errcode = '23514';
  end if;

  v_expected_publication_id := 'weekly_archive_' || substring(
    encode(
      extensions.digest(
        convert_to(
          '{"admin_artifact_sha256":"' || (p_publication ->> 'admin_artifact_sha256')
          || '","evaluation_artifact_sha256":"'
          || (p_publication ->> 'evaluation_artifact_sha256')
          || '","evaluation_id":"' || (p_publication ->> 'evaluation_id')
          || '","format_version":"' || (p_publication ->> 'format_version')
          || '","public_artifact_sha256":"'
          || (p_publication ->> 'public_artifact_sha256')
          || '","round_id":"' || (p_publication ->> 'round_id')
          || '","source_snapshot_sha256":"'
          || (p_publication ->> 'source_snapshot_sha256') || '"}',
          'UTF8'
        ),
        'sha256'
      ),
      'hex'
    )
    from 1 for 32
  );
  if p_publication ->> 'publication_id' is distinct from v_expected_publication_id then
    raise exception 'retrospective publication_id is not deterministic'
      using errcode = '23514';
  end if;

  insert into public.weekly_retrospective_publications (
    publication_id, round_id, campaign_id, environment, format_version,
    evaluation_id, evaluation_format_version,
    round_opens_at, round_closes_at, round_revealed_at,
    blind_manifest_sha256, private_index_sha256, reveal_manifest_sha256,
    reference_set_sha256, prediction_set_sha256,
    evaluation_artifact_sha256, item_count, choice_count,
    source_snapshot_object_uri, source_snapshot_sha256,
    source_snapshot_size_bytes, source_snapshot_media_type,
    public_artifact_object_uri, public_artifact_sha256,
    public_artifact_size_bytes, public_artifact_media_type,
    admin_artifact_object_uri, admin_artifact_sha256,
    admin_artifact_size_bytes, admin_artifact_media_type
  ) values (
    p_publication ->> 'publication_id',
    p_publication ->> 'round_id',
    p_publication ->> 'campaign_id',
    p_publication ->> 'environment',
    p_publication ->> 'format_version',
    p_publication ->> 'evaluation_id',
    p_publication ->> 'evaluation_format_version',
    (p_publication ->> 'round_opens_at')::timestamptz,
    (p_publication ->> 'round_closes_at')::timestamptz,
    (p_publication ->> 'round_revealed_at')::timestamptz,
    p_publication ->> 'blind_manifest_sha256',
    p_publication ->> 'private_index_sha256',
    p_publication ->> 'reveal_manifest_sha256',
    p_publication ->> 'reference_set_sha256',
    p_publication ->> 'prediction_set_sha256',
    p_publication ->> 'evaluation_artifact_sha256',
    (p_publication ->> 'item_count')::integer,
    (p_publication ->> 'choice_count')::integer,
    p_publication ->> 'source_snapshot_object_uri',
    p_publication ->> 'source_snapshot_sha256',
    (p_publication ->> 'source_snapshot_size_bytes')::bigint,
    p_publication ->> 'source_snapshot_media_type',
    p_publication ->> 'public_artifact_object_uri',
    p_publication ->> 'public_artifact_sha256',
    (p_publication ->> 'public_artifact_size_bytes')::bigint,
    p_publication ->> 'public_artifact_media_type',
    p_publication ->> 'admin_artifact_object_uri',
    p_publication ->> 'admin_artifact_sha256',
    (p_publication ->> 'admin_artifact_size_bytes')::bigint,
    p_publication ->> 'admin_artifact_media_type'
  )
  on conflict (publication_id) do nothing;

  select * into v_publication
    from public.weekly_retrospective_publications
   where publication_id = p_publication ->> 'publication_id';
  if not found then
    raise exception 'retrospective publication identity is bound to different content'
      using errcode = '23505';
  end if;
  return v_publication;
end;
$$;

revoke all on function public.register_weekly_retrospective_publication(jsonb, text)
  from public;
revoke all on function public.register_weekly_retrospective_publication(jsonb, text)
  from anon;
revoke all on function public.register_weekly_retrospective_publication(jsonb, text)
  from authenticated;
grant execute on function public.register_weekly_retrospective_publication(jsonb, text)
  to service_role;

comment on function public.register_weekly_retrospective_publication(jsonb, text) is
  'Atomically validates source rows and registers one deterministic immutable retrospective publication.';

commit;
