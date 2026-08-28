# Production mirror parity

The default branch of `rafwiewiora/foldarium` is the open-source mirror of the
browser application served at `https://www.foldarium.org`. The canonical
deployment revision lives in the private operational repository and is recorded
separately in each production handoff.

Production supplies configuration and credentials through its environment.
Those values, access gates, routing adapters, deployment-provider configuration,
and spend-producing schedules are not part of the public source tree. Shared
browser changes should be mirrored here through review after the operational
change is accepted.

## Verify a deployment

From the exact source revision being deployed, run:

```bash
npm run parity:production -- --origin https://www.foldarium.org
```

The check compares SHA-256 digests for browser modules that are expected to be
identical across the public mirror and production. Deployment-specific HTML
shells are excluded because production may add an access gate or route adapter.
The check also validates the public shape and production environment marker
returned by `/api/config` without recording operator-specific values.

Run the check only after all files from one source revision are live; a
deployment in progress can briefly contain mixed cache generations.

## Data boundary

The production database and object store are runtime state, not Git source.
Previously tracked legacy/demo data are preserved in the
[`foldarium-data`](https://github.com/rafwiewiora/foldarium-data) release
`legacy-public-v1`. That release is optional and is not a snapshot of the live
weekly database.

Every production handoff should record:

- the private operational production commit;
- the corresponding public mirror commit;
- the database migration head;
- the pipeline/control-plane version;
- any independently versioned data release used for demos or analysis.
