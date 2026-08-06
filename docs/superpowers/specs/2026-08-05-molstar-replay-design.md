# Mol* Snapshot Replay Design

## Goal

Record the molecular viewer state leading up to each locked quiz answer and let a researcher replay one answer trace at a time. Store JSON only; do not capture screenshots or video.

## Scope

This first version favors a small implementation over strong administrator security or behavioral analytics. It records resulting Mol* state, not semantic labels for every gesture.

Recording starts after a question is loaded and stops when the user presses **Lock in answer**, before correctness is revealed. Post-reveal exploration is not recorded.

## Trace Format

Add a nullable `viewer_trace jsonb` column to `quiz_answers`. A trace is:

```json
{
  "version": 1,
  "molstar_version": "4.6.0",
  "duration_ms": 12450,
  "truncated": false,
  "snapshots": [
    {
      "t_ms": 0,
      "kind": "state",
      "snapshot": {}
    },
    {
      "t_ms": 1380,
      "kind": "camera",
      "camera": {}
    }
  ]
}
```

`state` entries contain a serialized Mol* data tree, structure focus, structure selection, and camera. `camera` entries contain only the camera snapshot. Downloaded structures normally remain URL-backed; raw structures created in the browser, such as merged H-bond PDB data, can appear as JSON text in the tree.

The trace contains at most 100 entries. Once that limit is reached, recording stops and `truncated` becomes `true`.

## Recording

Create a focused recorder module with `start()`, `captureState()`, and `stop()` operations.

1. `start()` records the question-relative start time and captures the initial data tree plus camera.
2. After a Foldarium action rebuilds the scene—display mode, pose navigation, clustering, protein mode, or H-bond visibility—the caller awaits the rebuild and then calls `captureState()`.
3. Subscribe to Mol* camera changes. Debounce changes for 100 ms and append a camera-only snapshot when rotation, pan, or zoom settles.
4. Subscribe to Mol* focus and structure-selection changes. Debounce them together for 100 ms and append a state snapshot so residue focus and nearby-residue representations can be restored.
5. Programmatic replay and answer reveal do not run through the recorder.
6. `stop()` flushes pending focus, selection, and camera captures, then returns an immutable trace.

The answer-lock path stops the recorder before modifying reveal state. The completed trace is passed into the existing answer record and persisted by the current retry queue. If trace serialization fails, the answer is still stored with `viewer_trace: null`.

## Database

Create a new migration that:

- adds nullable `viewer_trace jsonb` to `public.quiz_answers`;
- allows `null` or a JSON object;
- requires `version = 1` and a JSON array at `snapshots` when a trace is present.

Existing answer ownership and append-only RLS policies remain unchanged. Because users already insert their own answer rows, no new client privileges are required.

## Replay API

Add one Node Vercel Function at `api/replay.js`. It accepts POST requests with:

```json
{
  "password": "...",
  "action": "sessions"
}
```

or:

```json
{
  "password": "...",
  "action": "answers",
  "session_id": "..."
}
```

The function:

- compares `password` with `REPLAY_PASSWORD` using a timing-safe comparison;
- returns `401` for an invalid password;
- uses `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` only on the server;
- queries Supabase REST directly with `fetch`, adding no database dependency;
- lists up to 100 recent sessions with minimal metadata;
- returns only traced answers for one validated session UUID;
- sets `Cache-Control: no-store`;
- never returns environment values or privileged credentials.

The password is sent in POST bodies and kept only in replay-page memory. There are no accounts, cookies, rate limits, or audit logs in this version. The public quiz password is unrelated to replay access.

## Replay Page

Add `replay.html` and `replay.js`.

1. The researcher enters the replay password.
2. The page requests recent anonymous quiz sessions.
3. Selecting a session requests its traced answers.
4. Selecting an answer creates or clears a Mol* 4.6.0 viewer and replays that trace.
5. Playback applies entries according to their original relative `t_ms`. If loading a data tree takes longer than the next scheduled event, playback continues immediately rather than adding more delay.
6. State entries use Mol* snapshot restoration; camera entries animate to the recorded endpoint over a short transition.

The UI shows session UUID, anonymous user UUID, source, difficulty, timestamps, item ID, question index, and answer correctness. It supports one selected answer at a time.

## Failure Behavior

- Snapshot capture errors are logged and skipped without interrupting quiz play.
- Trace persistence uses the existing durable retry and dead-letter behavior.
- Missing traces do not affect answer persistence.
- Missing replay environment variables produce a generic server configuration error.
- Invalid or unsupported traces show an error in the replay page without affecting other records.

## Security Boundary

The replay password and Supabase server credential must remain Vercel environment variables. The server credential must never appear in browser files or Git.

This shared-password design is intentionally minimal. It does not provide per-researcher identity, brute-force protection, access auditing, or protection from a malicious quiz client submitting a fabricated Mol* tree. Those limitations are acceptable only for the current limited-access deployment.

## Testing

- Recorder unit tests cover initial state, data-tree captures, debounced camera captures, stopping before reveal, entry limits, and capture failures.
- Persistence tests verify `viewer_trace` normalization and that answer persistence survives trace serialization failure.
- API tests cover valid and invalid passwords, missing configuration, action validation, UUID validation, query shape, response filtering, and credential non-disclosure.
- Replay tests verify state/camera application order, relative timing, cancellation, and unsupported trace handling.
- Existing 17 backend tests remain passing.
- Local static-server smoke tests verify the quiz still works and `replay.html` loads.
- A Vercel-connected smoke test verifies that a real answer trace can be retrieved and replayed.

## Deployment

Configure these Vercel environment variables:

- `REPLAY_PASSWORD`
- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`

Apply the new Supabase migration before deploying code that sends traces. Keep Mol* pinned to 4.6.0 while stored snapshots use `molstar_version: "4.6.0"`.

## Out of Scope

- Images or video
- Semantic interaction analytics
- Full-session playback across multiple answers
- Post-reveal recording
- Per-researcher accounts and permissions
- Login sessions, cookies, rate limiting, and audit logs
- Cross-version Mol* snapshot migration
