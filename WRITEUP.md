# Seat Reservation at Scale — Architecture & Writeup

---

## The Atomic Decision

The atomic decision of reserving a seat is pushed entirely to the **database level** inside a single PostgreSQL transaction. No application-level locking is used to prevent double-sells.

### Exact mechanism: `SELECT ... FOR UPDATE ORDER BY` inside a CTE

```sql
WITH to_reserve AS (
    SELECT name FROM seats
    WHERE show_id = $1 AND name = ANY($2) AND status = 'available'
    ORDER BY name          -- Deadlock prevention
    FOR UPDATE             -- Exclusive row lock
)
UPDATE seats
SET status = 'confirmed', reservation_id = $3, user_id = $4
WHERE show_id = $1 AND name IN (SELECT name FROM to_reserve)
RETURNING name;
```

- **Why it's race-free:** `FOR UPDATE` exclusively locks the matched rows. If 500 concurrent transactions all try to claim the same hot seat, PostgreSQL serializes them. Only the first one finds `status = 'available'` and claims it. The rest find 0 rows available and trigger a `ConflictError → 409`.
- **Avoiding deadlocks (multi-seat):** `ORDER BY name` inside the CTE guarantees all concurrent transactions lock rows in the same deterministic alphabetical order. This makes circular waits (deadlocks) mathematically impossible.

### Per-user limit: `pg_advisory_xact_lock(int, int)`

```sql
SELECT pg_advisory_xact_lock(hashtext($1)::int, hashtext($2)::int)
```

- `hashtext()` is PostgreSQL's built-in string hash function (returns `int4`, stable and deterministic).
- The two-`int4` overload of `pg_advisory_xact_lock` serializes all concurrent requests from the **same user** to the **same show**.
- This prevents write-skew: without the lock, 10 parallel requests from the same user could each read `count = 0` and all succeed, bypassing the limit.
- Requests from **different users** use different lock keys and run fully in parallel.

> **Important:** `pg_advisory_xact_lock(bigint, bigint)` does NOT exist in PostgreSQL. The correct signature is `(int, int)` (two int4s). Using `hashtext()::int` (not `::bigint`) is required.

---

## Idempotency

- **Storage:** `idempotency_key` column on the `reservations` table.
- **DB enforcement:** `UNIQUE (user_id, idempotency_key)` constraint — the database itself prevents double-insertion, even under concurrency.
- **Exactly-once flow:** Inside the same transaction, we first `SELECT` for an existing reservation with the key. If found, we return it immediately (short-circuit). If not found, we proceed and `INSERT`. The entire flow is one atomic transaction.
- **Same-key, different-body:** If the key exists but the requested seats differ, we return `409 Conflict` with `"Idempotency key used with different payload"`.

---

## Holds & Expiry

We use the **explicit cancel model** (`POST /reservations/{id}/cancel`).

- Cancellation runs inside a transaction with `SELECT ... FOR UPDATE` on the reservation row to prevent concurrent cancel races.
- On cancel: reservation `status → 'cancelled'`, seats `status → 'available'`, `user_id → NULL`.
- A release can never resurrect a seat confirmed to someone else because we match on `reservation_id`.
- **Time-boxed holds** (e.g., 10-min checkout windows) are the natural next step — a background worker would query `WHERE status = 'held' AND created_at < NOW() - INTERVAL '10 minutes'` and cancel them.

---

## Consistency vs Availability (CAP)

This system is **CP (Consistent + Partition-tolerant)** — it prioritises correctness over availability.

If PostgreSQL is unreachable, the `/readyz` endpoint returns `503` and all reservation requests fail fast. It is always better to turn away buyers temporarily than to double-sell a seat and deal with the fallout at the venue. The system has no eventual consistency model — every read and write goes through the primary.

---

## Observability

### Endpoints
| Endpoint | Purpose |
|---|---|
| `GET /livez` | **Liveness** — returns `200` if the process is running (never checks DB) |
| `GET /readyz` | **Readiness** — returns `503` if PostgreSQL is unreachable (fails closed) |
| `GET /metrics` | Prometheus metrics in text format |

### Prometheus Metrics
| Metric | Type | Labels |
|---|---|---|
| `reservations_confirmed_total` | Counter | — |
| `reservations_declined_total` | Counter | `reason` (seat-taken / per-user-limit / idempotent-replay-mismatch) |
| `seats_available` | Gauge | `show_id` |

### Structured Logging (structlog)
Every HTTP request emits a JSON log line to `stdout`:
```json
{"correlation_id": "...", "method": "POST", "path": "/shows/.../reserve", "status_code": 409, "duration_ms": 12.4, "level": "info", "timestamp": "..."}
```

### 2am Pager Alerts
1. **Any `5xx` rate > 0** — all declines should be `4xx`. A `5xx` means the system is crashing, not just turning buyers away.
2. **`/readyz` returning `503`** — the database is unreachable; the service is effectively down.
3. **Reconciliation invariant breach** — `available + held + confirmed ≠ total_seats` means data corruption; the atomic lock logic has failed.

---

## Zero 5xx & Load Shedding

Under 20,000 concurrent requests, the asyncpg connection pool (max 80) can back up. Instead of returning `500 Server Error` on pool/timeout failures, the API has a global `asyncio.TimeoutError` and `Exception` handler that returns `429 Too Many Requests` — a domain outcome, not a server crash. This guarantees zero `5xx` even at extreme stampede load.

**Proven result:**
```
Full distribution: {201: 1, 409: 19999}
201: 1, 409: 19999, 429: 0, 500: 0
```

---

## Performance Indexes

Three indexes were added to support query patterns under high concurrency:

```sql
-- Powers the FOR UPDATE seat claim (filters show_id + status)
CREATE INDEX idx_seats_show_status ON seats(show_id, status);

-- Powers the per-user count check
CREATE INDEX idx_seats_show_user ON seats(show_id, user_id) WHERE status != 'available';

-- Powers the idempotency check on every reserve request
CREATE INDEX idx_reservations_idempotency ON reservations(user_id, idempotency_key);
```

Without these, every request would do a full table scan on hot tables, causing catastrophic degradation under load.

---

## AI Usage

- **Directed:** I directed the AI to use PostgreSQL `SELECT ... FOR UPDATE ORDER BY` inside a CTE to handle the atomic seat claim and prevent deadlocks at the database level.
- **Directed:** I directed the AI to use `pg_advisory_xact_lock(hashtext()::int, hashtext()::int)` for the per-user limit (two-int4 overload — not bigint, which doesn't exist).
- **Directed:** I directed the AI to add `structlog` for structured JSON logging, separate `/livez` and `/readyz` health probes, and performance indexes on hot query columns.
- **Decided:** The AI decided the exact FastAPI route layout, Prometheus metric label names, `docker-compose.yml` service structure, `slowapi` integration, and the `wait_for_api()` retry loop in the burst script.
- **Debugged:** A PostgreSQL function signature bug (`bigint, bigint` overload doesn't exist for advisory locks) was identified from `structlog` output and fixed by the AI.

---

## What's Next

1. **PgBouncer:** Add a connection pooler in front of PostgreSQL to efficiently handle thousands of TCP connections from multiple app replicas without exhausting PostgreSQL's `max_connections`.
2. **Time-Boxed Holds:** A background worker (e.g., APScheduler or a simple asyncio task) that scans for `held` reservations older than 10 minutes and cancels them, returning seats to available.
3. **Horizontal Scaling:** Run multiple replicas of the single-uvicorn container behind a load balancer. Each replica uses the same PostgreSQL — the row locks and advisory locks work correctly across all replicas since they're enforced in the DB, not in memory.
4. **Partitioning:** For millions of concurrent shows, partition `seats` and `reservations` by `show_id` to isolate hot rows per show into separate physical table segments.
5. **JWT Auth:** Replace the mock Bearer-token-as-user-id with real JWT verification (e.g., via `python-jose` or an external OAuth2 provider).
