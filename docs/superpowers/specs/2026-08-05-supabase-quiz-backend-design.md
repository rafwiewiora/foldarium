# Supabase Quiz Backend Design

## Goal

Persist Foldarium quiz sessions and answer selections for research analysis while keeping the current static Vercel deployment. Identify repeat visits from the same browser without collecting personal information, and preserve a path to optional accounts later.

## Architecture

The browser uses Supabase Auth anonymous sign-in after the quiz is unlocked. Supabase restores the anonymous user UUID on later visits from the same browser profile. Clearing browser storage, using a private window, or changing devices creates a new anonymous identity until the user links an account.

The browser writes directly to Supabase Postgres with the public Supabase URL and publishable key. Row-level security is the authorization boundary: a user can insert and update only their own quiz sessions and insert answers only into sessions they own. Quiz answers are append-only for clients.

The existing local answer log becomes a small retry queue. Client-generated UUIDs make retries idempotent. Database failures never block quiz play.

## Data Model

`quiz_sessions` contains:

- client-generated session UUID
- Supabase user UUID
- quiz source and difficulty
- client start and completion timestamps
- server creation timestamp

`quiz_answers` contains:

- client-generated answer UUID
- owning session UUID
- question position and item ID
- source and difficulty
- selected pose sample or “none”
- correctness and RMSD
- AF3 baseline selection and correctness
- whether a correct pose existed and cluster count
- client answer timestamp and server creation timestamp

Supabase Auth is the participant registry, so a separate participant table is unnecessary.

## Security and Privacy

- Enable only anonymous authentication initially.
- Require an authenticated Supabase JWT for every insert.
- Enforce ownership with Postgres row-level security.
- Grant no client delete access and no answer update access.
- Store no name, email, IP-derived location, device fingerprint, or network identifier.
- Treat the publishable Supabase key as public; never expose the service-role key.
- Keep aggregate research access in the Supabase dashboard or another trusted server context.

This basic-RLS design prevents users from accessing another user’s rows but does not make browser-submitted scientific results tamper-proof. The client can inspect quiz answers and fabricate its own submissions. Server-side validation can be added later if that risk becomes material.

## Client Flow

1. Unlock the quiz and load the Supabase browser client.
2. Restore an existing anonymous auth session or create one.
3. On quiz start, generate a session UUID, enqueue its row, and flush.
4. On each answer reveal, preserve the existing local log, enqueue a UUID-keyed answer row, and flush.
5. On quiz completion, enqueue the session completion timestamp and flush.
6. Retry queued operations on startup and after later successful writes.

If Supabase is unavailable or unconfigured, the quiz remains fully playable and retains its current local log.

## Deployment and Configuration

Add a Supabase SQL migration containing tables, constraints, indexes, grants, and RLS policies. Add a small browser module for authentication, queueing, and writes. A public runtime configuration file supplies the Supabase project URL and publishable key; empty values disable remote persistence for local development.

Vercel continues deploying from GitHub without a framework migration or serverless function. Supabase project creation, anonymous-auth enablement, and the public configuration values are one-time dashboard steps documented in the README.

## Testing

- Database policy checks verify cross-user writes are rejected.
- Browser-module unit tests cover disabled configuration, stable payloads, queue retention, idempotent retries, and failure isolation.
- A manual local smoke test verifies the quiz remains playable with Supabase disabled.
- A connected smoke test verifies one browser identity owns multiple sessions and their answers.

## Out of Scope

- Public leaderboards
- Cross-device identity before account linking
- Email or OAuth account UI
- Administrative analytics UI
- Server-side answer validation, anti-bot protection, and rate limiting
- Collection of device or network metadata
