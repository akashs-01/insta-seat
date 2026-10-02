# Seat Reservation at Scale

A highly concurrent JSON HTTP API for seat reservations, built to survive ticket on-sale stampedes. Uses **FastAPI + PostgreSQL** with native row-level locking and advisory locks to guarantee correctness under 20,000 concurrent requests.

---

## Tech Stack

| Layer | Technology |
|---|---|
| API Framework | FastAPI 0.111 |
| ASGI Server | Uvicorn (single process, async event loop) |
| Database | PostgreSQL 15 (via asyncpg) |
| Logging | structlog (structured JSON) |
| Metrics | Prometheus (`/metrics`) |
| Rate Limiting | slowapi (IP-based, on admin endpoints) |
| DB GUI | pgAdmin 4 |

---

## Prerequisites
- Docker & Docker Compose

---

## Running Locally (via Docker Compose)

The entire stack — API, PostgreSQL, and pgAdmin — starts with a single command.

**1. Copy the env file and start all services:**
```bash
# The .env file is already created for local dev.
# Start everything:
docker-compose up -d --build
```

| Service | URL | Notes |
|---|---|---|
| **API** | `http://localhost:8000` | REST JSON API |
| **Swagger Docs** | `http://localhost:8000/docs` | Interactive API explorer |
| **pgAdmin** | `http://localhost:5050` | DB GUI (localhost only) |
| **Prometheus Metrics** | `http://localhost:8000/metrics` | Raw metrics |

**2. pgAdmin Login:**
- Login using the credentials defined in your `docker-compose.yml` (`PGADMIN_DEFAULT_EMAIL` / `PGADMIN_DEFAULT_PASSWORD`).
- DB Connection → Host: `db`, Port: `5432`. Use the Database, User, and Password defined in your `.env` file.

**3. Stop everything:**
```bash
docker-compose down
```

---

## API Endpoints

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/livez` | — | Liveness probe (process alive) |
| `GET` | `/readyz` | — | Readiness probe (DB reachable, fails 503) |
| `GET` | `/health` | — | Alias for `/readyz` |
| `GET` | `/metrics` | — | Prometheus metrics |
| `POST` | `/shows` | — | Create a show with named seats |
| `GET` | `/shows/{id}` | — | Show state + per-status seat counts |
| `POST` | `/shows/{id}/reserve` | ✅ Bearer | Reserve seat(s) — idempotent |
| `POST` | `/reservations/{id}/cancel` | ✅ Bearer | Cancel (owner only) |

> **Auth:** The Bearer token is the `user_id` in the mock auth. e.g., `Authorization: Bearer alice`

---

## The Burst Script (Load Testing)

Simulates the on-sale stampede and asserts all correctness requirements.

```bash
docker-compose run --rm -e BASE_URL=http://api:8000 api python burst.py
```

The script automatically waits for the API to be fully ready (polls `/readyz`) before firing.

### What it tests and asserts:

| Test | Assertion |
|---|---|
| 20,000 users → 75 seats (75% booking) | Exactly `75` get `201`, all others get `409`, `500 = 0` |
| 1 greedy user → 10 seats concurrently | At most `4` confirmed (per-user limit holds) |
| Final reconciliation | `available + held + confirmed == total_seats` always |

### Proven results (local Docker run):
```text
Traffic plan:
  🔥 Hot seats    ['A26', 'A27', 'A28'] → ~10012 requests (50% of traffic) — 3 seats available
  🪑 Normal seats A29–A100 → ~9988 requests (49% of traffic) — 72 seats available
  Expected winners: 3 (hot) + 72 (normal) = 75 confirmed bookings

Full distribution: {201: 75, 409: 19925}
Results for stampede: 201: 75, 409: 19925, 429: 0, 500: 0, conn-errors(-1): 0

  ✅ 75/75 seats successfully booked (100% of target capacity filled)
  🚫 19925 clean conflict declines (409 — seat already taken)

Per-user limit: 201: 4, 409: 6, 500: 0
Reconciliation: {'available': 21, 'held': 0, 'confirmed': 79} — Total: 100 ✅
```

---

## Observability

- **Metrics** — `GET /metrics` — Prometheus counters/gauges for confirmed, declined-by-reason, and seats available per show.
- **Health** — `GET /livez` (process) and `GET /readyz` (DB). The readiness probe fails closed with `503` if PostgreSQL is unreachable.
- **Logs** — All requests emit structured JSON to `stdout` with `correlation_id`, method, path, status code, and duration. View live: `docker-compose logs -f api`
