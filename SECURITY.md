# Security Policy

## Supported Versions

Only the latest `main` branch is supported. Older commits do not receive security fixes.

## Reporting a Vulnerability

Please **do not** open a public issue for security problems.

Email the maintainer at `dengyouming163@users.noreply.github.com` with:

- a description of the issue and its impact,
- reproduction steps or a proof of concept,
- the commit hash you tested,
- any suggested mitigation.

You can expect an acknowledgement within 3 business days. Confirmed issues are fixed
within 30 days, and the fix is credited in `CHANGELOG.md` unless you ask otherwise.

## Deployment Security Notes

The public demo runs behind an nginx sidecar with:

- HTTP Basic Auth on `/ui` and `/v1` (only `/health` is anonymous),
- per-IP request limiting (10 r/s, burst 20),
- a 50 MB upload cap enforced by both nginx and the API (`MAX_UPLOAD_BYTES`),
- **no TLS** — credentials travel in clear text. The showcase credential pair is published in the README, holds no secret, and is rotated by regenerating the htpasswd file; never reuse it for real data.

Operational hardening expected in any real deployment:

- multi-tenant isolation relies on PostgreSQL Row Level Security (migration `0007`);
  the application connects as the non-superuser `kb_app` because superusers bypass RLS,
- keep `.env` at mode `600` and never commit it,
- rotate demo credentials and switch to SSH keys after the initial setup,
- terminate TLS in front of the gateway before exposing real data.
