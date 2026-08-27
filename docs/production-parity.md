# Production parity

The default branch of `rafwiewiora/foldarium` is the canonical source for the
application served at `https://www.foldarium.org`.

Production supplies configuration and credentials through its environment.
Those values, routing adapters, deployment-provider configuration, and
spend-producing schedules are not part of the public source tree.

## Verify a deployment

From the exact source revision being deployed, run:

```bash
npm run parity:production -- --origin https://www.foldarium.org
```

The check compares SHA-256 digests for the weekly shell, application,
leaderboard, and retrospective assets. It also validates the public shape and
production environment marker returned by `/api/config` without recording
operator-specific values.

Run the check only after all files from one source revision are live; a
deployment in progress can briefly contain mixed cache generations.

## Data boundary

The production database and object store are runtime state, not Git source.
Previously tracked legacy/demo data are preserved in the
[`foldarium-data`](https://github.com/rafwiewiora/foldarium-data) release
`legacy-public-v1`. That release is optional and is not a snapshot of the live
weekly database.

Every production handoff should record:

- the public Foldarium commit;
- the database migration head;
- the pipeline/control-plane version;
- any independently versioned data release used for demos or analysis.
