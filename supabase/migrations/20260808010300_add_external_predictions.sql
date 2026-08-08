-- Normalized provenance for public third-party comparator predictions (CAMEO AF3).

begin;

create table if not exists public.external_prediction_sets (
  external_set_id text primary key,
  target_id text not null references public.targets(target_id) on delete cascade,
  provider text not null,
  method text not null,
  provider_server_id text,
  provider_target_id text not null,
  source_page_uri text not null,
  source_page_sha256 text not null check (source_page_sha256 ~ '^[0-9a-f]{64}$'),
  license text not null,
  import_manifest jsonb not null
    check (jsonb_typeof(import_manifest) is not distinct from 'object'),
  status text not null default 'imported'
    check (status in ('imported', 'evaluated', 'failed')),
  imported_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (target_id, provider, method, provider_server_id)
);

create table if not exists public.external_prediction_artifacts (
  artifact_id text primary key,
  external_set_id text not null
    references public.external_prediction_sets(external_set_id) on delete cascade,
  role text not null check (role in ('source_page', 'prediction', 'reference')),
  model_index integer check (model_index is null or model_index between 1 and 5),
  assembly_id integer check (assembly_id is null or assembly_id > 0),
  object_uri text not null,
  sha256 text not null check (sha256 ~ '^[0-9a-f]{64}$'),
  size_bytes bigint not null check (size_bytes >= 0),
  media_type text not null,
  source_uri text not null,
  metadata jsonb not null default '{}'::jsonb
    check (jsonb_typeof(metadata) is not distinct from 'object'),
  created_at timestamptz not null default now(),
  unique (external_set_id, role, model_index, assembly_id),
  check (
    (role = 'source_page' and model_index is null and assembly_id is null)
    or (role = 'prediction' and model_index is not null and assembly_id is null)
    or (role = 'reference' and model_index is null and assembly_id is not null)
  )
);

create index if not exists external_prediction_sets_target_idx
  on public.external_prediction_sets (target_id, provider, method);
create index if not exists external_prediction_artifacts_set_idx
  on public.external_prediction_artifacts (external_set_id);

drop trigger if exists external_prediction_sets_set_updated_at
  on public.external_prediction_sets;
create trigger external_prediction_sets_set_updated_at
before update on public.external_prediction_sets
for each row execute function public.foldarium_set_updated_at();

create or replace function public.register_external_prediction_set(
  p_set jsonb,
  p_artifacts jsonb
)
returns public.external_prediction_sets
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_set public.external_prediction_sets%rowtype;
  v_set_id text;
begin
  if jsonb_typeof(p_set) is distinct from 'object'
     or jsonb_typeof(p_artifacts) is distinct from 'array'
     or jsonb_array_length(p_artifacts) < 2 then
    raise exception 'external prediction set/artifacts are invalid'
      using errcode = '22023';
  end if;
  v_set_id := p_set ->> 'external_set_id';
  if nullif(v_set_id, '') is null
     or nullif(p_set ->> 'target_id', '') is null
     or nullif(p_set ->> 'provider', '') is null
     or nullif(p_set ->> 'method', '') is null
     or nullif(p_set ->> 'provider_target_id', '') is null
     or nullif(p_set ->> 'source_page_uri', '') is null
     or p_set ->> 'source_page_sha256' !~ '^[0-9a-f]{64}$'
     or jsonb_typeof(p_set -> 'import_manifest') is distinct from 'object' then
    raise exception 'external prediction set identity is invalid'
      using errcode = '22023';
  end if;
  if exists (
    select 1
      from jsonb_to_recordset(p_artifacts) as artifact(
        artifact_id text, external_set_id text, role text, model_index integer,
        assembly_id integer, object_uri text, sha256 text, size_bytes bigint,
        media_type text, source_uri text, metadata jsonb
      )
     where nullif(artifact_id, '') is null
        or external_set_id is distinct from v_set_id
        or role not in ('source_page', 'prediction', 'reference')
        or (role = 'prediction' and model_index not between 1 and 5)
        or (role = 'reference' and assembly_id < 1)
        or (role = 'source_page' and (model_index is not null or assembly_id is not null))
        or (role = 'prediction' and assembly_id is not null)
        or (role = 'reference' and model_index is not null)
        or nullif(object_uri, '') is null
        or sha256 !~ '^[0-9a-f]{64}$'
        or size_bytes < 0
        or nullif(media_type, '') is null
        or nullif(source_uri, '') is null
        or jsonb_typeof(coalesce(metadata, '{}'::jsonb)) is distinct from 'object'
  ) then
    raise exception 'external prediction artifact metadata is invalid'
      using errcode = '22023';
  end if;

  insert into public.external_prediction_sets (
    external_set_id, target_id, provider, method, provider_server_id,
    provider_target_id, source_page_uri, source_page_sha256, license,
    import_manifest, status
  )
  select
    external_set_id, target_id, provider, method, provider_server_id,
    provider_target_id, source_page_uri, source_page_sha256, license,
    import_manifest, coalesce(status, 'imported')
  from jsonb_to_record(p_set) as incoming(
    external_set_id text, target_id text, provider text, method text,
    provider_server_id text, provider_target_id text, source_page_uri text,
    source_page_sha256 text, license text, import_manifest jsonb, status text
  )
  on conflict (external_set_id) do nothing;

  select * into v_set from public.external_prediction_sets
   where external_set_id = v_set_id for update;
  if v_set.target_id <> p_set ->> 'target_id'
     or v_set.source_page_sha256 <> p_set ->> 'source_page_sha256'
     or v_set.import_manifest <> p_set -> 'import_manifest' then
    raise exception 'external prediction identity is bound to different content'
      using errcode = '23505';
  end if;

  insert into public.external_prediction_artifacts (
    artifact_id, external_set_id, role, model_index, assembly_id,
    object_uri, sha256, size_bytes, media_type, source_uri, metadata
  )
  select
    artifact_id, external_set_id, role, model_index, assembly_id,
    object_uri, sha256, size_bytes, media_type, source_uri,
    coalesce(metadata, '{}'::jsonb)
  from jsonb_to_recordset(p_artifacts) as artifact(
    artifact_id text, external_set_id text, role text, model_index integer,
    assembly_id integer, object_uri text, sha256 text, size_bytes bigint,
    media_type text, source_uri text, metadata jsonb
  )
  on conflict (artifact_id) do nothing;

  if exists (
    select 1
      from jsonb_to_recordset(p_artifacts) as incoming(
        artifact_id text, external_set_id text, object_uri text, sha256 text
      )
      join public.external_prediction_artifacts as stored using (artifact_id)
     where stored.external_set_id <> incoming.external_set_id
        or stored.object_uri <> incoming.object_uri
        or stored.sha256 <> incoming.sha256
  ) then
    raise exception 'external artifact identity is bound to different content'
      using errcode = '23505';
  end if;
  return v_set;
end;
$$;

alter table public.external_prediction_sets enable row level security;
alter table public.external_prediction_artifacts enable row level security;
revoke all on table public.external_prediction_sets from public;
revoke all on table public.external_prediction_artifacts from public;
revoke all on function public.register_external_prediction_set(jsonb, jsonb) from public;

do $$
begin
  if exists (select 1 from pg_roles where rolname = 'anon') then
    revoke all on table public.external_prediction_sets,
      public.external_prediction_artifacts from anon;
    revoke execute on function public.register_external_prediction_set(jsonb, jsonb) from anon;
  end if;
  if exists (select 1 from pg_roles where rolname = 'authenticated') then
    revoke all on table public.external_prediction_sets,
      public.external_prediction_artifacts from authenticated;
    revoke execute on function public.register_external_prediction_set(jsonb, jsonb)
      from authenticated;
  end if;
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    grant select, insert, update, delete on table public.external_prediction_sets,
      public.external_prediction_artifacts to service_role;
    grant execute on function public.register_external_prediction_set(jsonb, jsonb)
      to service_role;
  end if;
end;
$$;

comment on table public.external_prediction_sets is
  'Public comparator predictions imported with source page, license, and immutable artifacts.';

commit;
