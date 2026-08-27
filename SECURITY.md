# Security policy

Please report suspected vulnerabilities privately through GitHub's
security-advisory feature for this repository. Do not open a public issue with
credentials, participant data, unpublished structures, or exploit details.

## Supported version

Security fixes target the current `main` branch.

## Deployment responsibility

This repository ships no authentication proxy, production secrets, or hosted
deployment profile. Operators are responsible for:

- placing privileged endpoints behind authenticated access;
- applying and testing Supabase RLS policies;
- keeping service-role keys and ingest/HMAC/replay secrets server-side;
- rate limiting, monitoring, backup, retention, and incident response;
- reviewing upstream model and dependency security notices.

The browser application intentionally has no client-side password gate. A
password embedded in HTML or JavaScript is public and is not an access control.
