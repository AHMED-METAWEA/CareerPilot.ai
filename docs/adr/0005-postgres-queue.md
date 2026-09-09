# ADR 0005 — Postgres queue over Redis at V1

**Status:** accepted · **Date:** 2026-09-08

## Context

The worker needs a durable task queue for discovery, deduplication, detail
fetching, verification and matching.

## Decision

`SELECT … FOR UPDATE SKIP LOCKED` over a `task_queue` table in the same
database as the domain data.

## Consequences

- Queue state is transactionally consistent with domain state: a task that
  writes postings and enqueues follow-up work either does both or neither.
- The queue is observable through ordinary SQL — no separate dashboard.
- A crashed worker releases its claim with its transaction; `reap_stale` covers
  the narrow case of a process killed after the claim committed.
- Idempotency is explicit: `dedup_key` carries a partial unique index over
  pending and running rows, so an over-eager scheduler cannot double-queue.
- The cost is polling latency (2 s idle sleep) and queue traffic hitting the
  primary database. Redis arrives when pub/sub or a genuine cache need does.

## Deviation from §14

The plan sketches this module at `app/workers/queue.py`. It is implemented at
`app/db/queue.py` instead, because the import contract in §14.1 forbids
`services` and `api` from importing the worker layer — and both need to enqueue
work (the ingestion service queues deduplication; the admin endpoint triggers a
run). The queue is one table and the SQL that manipulates it, so the persistence
layer is where it belongs; `app/workers/` keeps the scheduler and the task
handlers that consume it. Enforced by `lint-imports` in CI.
