# Seat Reservation at Scale

A highly concurrent API for seat reservations, built to withstand ticket on-sale stampedes using native PostgreSQL concurrency controls.

## Prerequisites
- Docker & Docker Compose
- Python 3.11+ (for the load testing script)

## Running the Application Locally (via Docker Compose)

The easiest way to run the entire stack (Database, API, and pgAdmin) is using Docker Compose:

1. **Start all services:**
   ```bash
   docker-compose up -d --build
   ```
   * The API will be available at: `http://localhost:8000`
   * pgAdmin (DB viewer) will be available at: `http://localhost:5050` (Login: `admin@admin.com` / `root`)

## The Burst Script (Load Testing)

To prove correctness under load, run the provided `burst.py` script. This script fires a massive concurrent stampede at a single "hot seat" and verifies the final state.

1. **Run the script against the docker container:**
   ```bash
   docker-compose run --rm -e BASE_URL=http://api:8000 api python burst.py
   ```

### What the script tests:
1. **The Hot Seat Stampede**: 20,000 users attempt to book `Seat A12` at the exact same millisecond. The script verifies that exactly 1 user gets a `201 Created`, exactly 19,999 users get a `409/429 Conflict`, and zero requests result in `500 Server Errors`.
2. **Per-User Limits**: 1 user attempts to book 10 different seats simultaneously. The script verifies that the user is capped at exactly 4 confirmed seats.
3. **Reconciliation**: The script queries the total show state to ensure `available + confirmed == total_seats` perfectly matches the expected numbers, proving no seats were dropped or double-sold.

## Observability
- **Metrics**: Available at `GET /metrics` in Prometheus format.
- **Health**: Available at `GET /health` (fails closed if the DB is down).
- **Logs**: All requests emit structured JSON logs to `stdout` containing a `correlation_id` and timing data.
