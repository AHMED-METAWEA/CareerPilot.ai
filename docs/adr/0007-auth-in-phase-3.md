# ADR 0007 — Development header before Phase 3, bearer tokens after

**Status:** accepted · **Date:** 2026-09-09

## Context

Phases 1 and 2 needed a caller identity — a CV belongs to someone, a match is
computed for someone — but the plan puts multi-user authentication in Phase 3,
and building it earlier would have delayed the parts of the system that carry
the actual risk.

## Decision

Phase 1 used an `X-User-Id` header, validated against the users table and
refused outright when `CAREERPILOT_ENV=production`. Phase 3 replaced it with
Argon2id passwords and JWT bearer tokens.

## Consequences

- The interim mechanism was deliberately unmistakable for authentication: a
  header anyone can set, that refuses to run in production. A half-built login
  form would have been worse — it would have *looked* like a boundary.
- The switch was one dependency (`current_user`) and one test fixture. Nothing
  in the services or the domain knew about the header, because identity arrives
  as a `user_id` argument rather than being read from a request.
- Every query is scoped by `user_id` at the repository layer rather than the
  route layer (§16.2), so a handler that forgets to filter cannot leak another
  user's data. `test_another_users_application_is_invisible` is the check.

## What is deliberately not here

Password reset, email verification and OAuth. Each needs outbound email, which
§16.4 gates behind an unsubscribe mechanism, sender identification and a consent
record — the same checkpoint the digest is waiting on.
