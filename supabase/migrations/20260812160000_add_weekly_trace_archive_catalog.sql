-- Server-only catalog for verified cold archives of bulky weekly replay data.
--
-- This migration is intentionally schema-only.  It exposes no browser RPC,
-- uploads nothing, and defines no deletion function.  Future archive workers
-- must prove exact membership and integrity before advancing lifecycle state.

begin;

-- session_id is already the primary key, but PostgreSQL requires an exact
-- unique key for the composite archive foreign key.  Keeping round_id in that
-- key makes a future archive writer fail closed if it ever pairs a session
-- with the wrong otherwise-valid weekly round.
alter table public.weekly_quiz_sessions
  add constraint weekly_quiz_sessions_session_id_round_id_key
  unique (session_id, round_id);

create schema if not exists private;
revoke all on schema private from public;

create table private.weekly_trace_archive_jobs (
  archive_job_id uuid primary key,
  round_id text not null
    references public.weekly_quiz_rounds(round_id),
  cutoff_at timestamptz not null,
  state text not null default 'planned'
    check (state in ('planned', 'exported', 'verified', 'uploaded', 'failed')),
  -- Set by a future operator before any external upload is allowed.  This
  -- foundation itself only ever creates/plans dry-run records.
  dry_run boolean not null default true,
  session_count integer not null default 0 check (session_count >= 0),
  vote_attempt_count bigint not null default 0 check (vote_attempt_count >= 0),
  trace_batch_count bigint not null default 0 check (trace_batch_count >= 0),
  trace_entry_count bigint not null default 0 check (trace_entry_count >= 0),
  omitted_entry_count bigint not null default 0 check (omitted_entry_count >= 0),
  dead_letter_entry_count bigint not null default 0 check (dead_letter_entry_count >= 0),
  accounted_omitted_sequence_count bigint not null default 0
    check (accounted_omitted_sequence_count >= 0),
  source_bytes bigint not null default 0 check (source_bytes >= 0),
  archive_bytes bigint not null default 0 check (archive_bytes >= 0),
  error_code text,
  error_detail text,
  created_at timestamptz not null default clock_timestamp(),
  exported_at timestamptz,
  verified_at timestamptz,
  uploaded_at timestamptz,
  check (cutoff_at <= created_at),
  check ((state <> 'exported') or exported_at is not null),
  check ((state <> 'verified') or (exported_at is not null and verified_at is not null)),
  check ((state <> 'uploaded') or (
    not dry_run
    and
    exported_at is not null and verified_at is not null and uploaded_at is not null
  )),
  check ((state = 'failed') or (error_code is null and error_detail is null))
);

create index weekly_trace_archive_jobs_round_created_idx
  on private.weekly_trace_archive_jobs (round_id, created_at desc);

create table private.weekly_trace_archives (
  archive_id uuid primary key,
  archive_job_id uuid not null
    references private.weekly_trace_archive_jobs(archive_job_id),
  session_id uuid not null,
  round_id text not null,
  participant_hash text not null check (participant_hash ~ '^[0-9a-f]{64}$'),
  display_name_hash text not null check (display_name_hash ~ '^[0-9a-f]{64}$'),
  format_version text not null
    check (format_version = 'foldarium.weekly-session-trace-archive/v1'),
  exporter_version text not null,
  compression text not null check (compression in ('gzip', 'zstd')),
  content_sha256 text not null check (content_sha256 ~ '^[0-9a-f]{64}$'),
  archive_sha256 text not null check (archive_sha256 ~ '^[0-9a-f]{64}$'),
  membership_sha256 text not null check (membership_sha256 ~ '^[0-9a-f]{64}$'),
  uncompressed_bytes bigint not null check (uncompressed_bytes > 0),
  archive_bytes bigint not null check (archive_bytes > 0),
  vote_attempt_count integer not null check (vote_attempt_count >= 0),
  trace_batch_count integer not null check (trace_batch_count >= 0),
  trace_entry_count bigint not null check (trace_entry_count >= 0),
  omitted_entry_count bigint not null default 0 check (omitted_entry_count >= 0),
  dead_letter_entry_count bigint not null default 0 check (dead_letter_entry_count >= 0),
  accounted_omitted_sequence_count bigint not null default 0
    check (accounted_omitted_sequence_count >= 0),
  visit_count integer not null check (visit_count >= 0),
  sequence_gaps integer not null default 0 check (sequence_gaps = 0),
  first_submitted_at timestamptz,
  last_submitted_at timestamptz,
  object_uri text,
  object_version text,
  state text not null default 'exported'
    check (state in ('exported', 'verified', 'available', 'retired')),
  exported_at timestamptz not null,
  verified_at timestamptz,
  available_at timestamptz,
  last_verified_at timestamptz,
  created_at timestamptz not null default clock_timestamp(),
  unique (session_id, content_sha256),
  foreign key (session_id, round_id)
    references public.weekly_quiz_sessions(session_id, round_id),
  check (
    (first_submitted_at is null and last_submitted_at is null)
    or (first_submitted_at is not null and last_submitted_at >= first_submitted_at)
  ),
  check ((state = 'exported') or verified_at is not null),
  check ((state <> 'available') or (
    verified_at is not null and available_at is not null
    and nullif(object_uri, '') is not null and nullif(object_version, '') is not null
  ))
);

create index weekly_trace_archives_session_created_idx
  on private.weekly_trace_archives (session_id, created_at desc);
create index weekly_trace_archives_round_state_idx
  on private.weekly_trace_archives (round_id, state, created_at);

-- Exact source-row membership is the proof used by later export/drift checks.
-- It also prevents the same source row from being represented twice inside one
-- archive.  This table is not a purge queue and confers no deletion authority.
create table private.weekly_trace_archive_members (
  archive_id uuid not null
    references private.weekly_trace_archives(archive_id),
  source_kind text not null
    check (source_kind in ('trace_batch', 'vote_attempt')),
  source_id uuid not null,
  item_id text not null check (char_length(item_id) between 1 and 200),
  question_index integer not null check (question_index >= 0),
  submitted_at timestamptz not null,
  visit_id uuid,
  first_sequence integer,
  last_sequence integer,
  payload_sha256 text not null check (payload_sha256 ~ '^[0-9a-f]{64}$'),
  payload_bytes integer not null check (payload_bytes > 0),
  archive_record_ordinal integer not null check (archive_record_ordinal >= 0),
  visit_ordinal integer,
  visit_batch_ordinal integer,
  question_revision_ordinal integer,
  primary key (archive_id, source_kind, source_id),
  check (
    (source_kind = 'trace_batch'
      and visit_id is not null
      and first_sequence is not null and first_sequence >= 0
      and last_sequence is not null and last_sequence >= first_sequence
      and visit_ordinal is not null and visit_ordinal >= 0
      and visit_batch_ordinal is not null and visit_batch_ordinal >= 0
      and question_revision_ordinal is null)
    or
    (source_kind = 'vote_attempt'
      and visit_id is null and first_sequence is null and last_sequence is null
      and visit_ordinal is null and visit_batch_ordinal is null
      and question_revision_ordinal is not null and question_revision_ordinal >= 0)
  )
);

create index weekly_trace_archive_members_source_idx
  on private.weekly_trace_archive_members (source_kind, source_id);
create unique index weekly_trace_archive_members_record_order_idx
  on private.weekly_trace_archive_members (archive_id, archive_record_ordinal);
create index weekly_trace_archive_members_session_order_idx
  on private.weekly_trace_archive_members (archive_id, submitted_at, source_kind, source_id);

-- Compact replay index: a replay server can find a question/visit and its
-- verified sequence envelope without storing the bulky event payload in the
-- hot database.
create table private.weekly_trace_archive_visits (
  archive_id uuid not null
    references private.weekly_trace_archives(archive_id),
  visit_id uuid not null,
  visit_ordinal integer not null check (visit_ordinal >= 0),
  item_id text not null check (char_length(item_id) between 1 and 200),
  question_index integer not null check (question_index >= 0),
  first_sequence integer not null check (first_sequence = 0),
  last_sequence integer not null check (last_sequence >= first_sequence),
  batch_count integer not null check (batch_count > 0),
  trace_entry_count integer not null check (trace_entry_count > 0),
  omitted_entry_count integer not null default 0 check (omitted_entry_count >= 0),
  dead_letter_entry_count integer not null default 0 check (dead_letter_entry_count >= 0),
  accounted_omitted_sequence_count integer not null default 0
    check (accounted_omitted_sequence_count >= 0),
  first_submitted_at timestamptz not null,
  last_submitted_at timestamptz not null,
  primary key (archive_id, visit_id),
  unique (archive_id, visit_ordinal),
  check (last_submitted_at >= first_submitted_at)
);

create index weekly_trace_archive_visits_question_idx
  on private.weekly_trace_archive_visits (archive_id, question_index, visit_id);

revoke all on table private.weekly_trace_archive_jobs from public;
revoke all on table private.weekly_trace_archives from public;
revoke all on table private.weekly_trace_archive_members from public;
revoke all on table private.weekly_trace_archive_visits from public;

do $$
begin
  if exists (select 1 from pg_roles where rolname = 'anon') then
    revoke all on table private.weekly_trace_archive_jobs from anon;
    revoke all on table private.weekly_trace_archives from anon;
    revoke all on table private.weekly_trace_archive_members from anon;
    revoke all on table private.weekly_trace_archive_visits from anon;
  end if;
  if exists (select 1 from pg_roles where rolname = 'authenticated') then
    revoke all on table private.weekly_trace_archive_jobs from authenticated;
    revoke all on table private.weekly_trace_archives from authenticated;
    revoke all on table private.weekly_trace_archive_members from authenticated;
    revoke all on table private.weekly_trace_archive_visits from authenticated;
  end if;
end;
$$;

comment on table private.weekly_trace_archive_jobs is
  'Server-only archive workflow journal; schema foundation only, with no deletion RPC.';
comment on table private.weekly_trace_archives is
  'Verified per-session cold archive manifests. Vote metadata and comments remain hot.';
comment on table private.weekly_trace_archive_members is
  'Exact, checksummed source-row membership for archive verification and drift detection.';
comment on table private.weekly_trace_archive_visits is
  'Compact replay index for a verified archive; contains no trace-event payload.';

commit;
