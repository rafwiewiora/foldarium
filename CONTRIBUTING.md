# Contributing

## Development setup

1. Install Node.js 20+ and Python 3.11 or 3.12.
2. Run `npm test`.
3. Create a virtual environment and run
   `python -m unittest discover -s pipeline/tests -v`.
4. Install `./pipeline[evaluation]` before changing scientific evaluation code.

Keep pull requests focused and include tests for behavior changes. Do not commit
credentials, private structures, model weights, generated prediction archives,
participant exports, or deployment profiles.

## Scientific changes

Changes to selection, alignment, clustering, RMSD, scoring, or reveal behavior
must include:

- a versioned contract or policy change when output meaning changes;
- deterministic fixtures covering the affected edge case;
- provenance for any new reference data;
- evidence that blindness and private-before-reveal boundaries are preserved.

Do not silently reinterpret an already published round.

## Security and privacy

Use synthetic identities and placeholder credentials in tests. New privileged
endpoints must fail closed, minimize returned fields, and include authorization,
RLS, replay, idempotency, and redaction tests.

By submitting a contribution, you agree that it may be distributed under the
repository's MIT License and confirm that you have the right to contribute it.
