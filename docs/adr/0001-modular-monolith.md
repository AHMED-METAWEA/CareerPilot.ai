# ADR 0001 — Modular monolith over microservices

**Status:** accepted · **Date:** 2026-09-08

## Context

The system is built and operated by one engineer, on free-tier infrastructure,
with a target of $0 monthly cost at 50 users (§20.4).

## Decision

One API deployable, one worker deployable, one database. Domain boundaries are
enforced by Python package structure and import discipline (`import-linter`
contracts in `pyproject.toml`), not by network hops.

## Consequences

- No service mesh, no distributed tracing, no cross-service schema versioning,
  no eventual-consistency reasoning for problems that are not distributed.
- The boundaries are still real: `app.domain` may not import `app.adapters`,
  `app.services`, `app.db`, `app.api`, or any I/O library. CI fails on a
  violation, so the boundary cannot erode quietly.
- Extracting a module into a service later is mechanical rather than
  archaeological, because the seam already exists.
- The cost is a shared deployment unit: a change to the worker redeploys the
  API image too.
