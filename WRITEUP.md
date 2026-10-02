# Seat Reservation at Scale - Architecture & Writeup

## The Atomic Decision
The atomic decision of reserving a seat is pushed down to the database level using PostgreSQL row-level locks within a single transaction. 
- **Exact mechanism:** When a reservation request comes in, the application uses a `SELECT ... FOR UPDATE` query with a `WHERE status = 'available'` clause inside a CTE (Common Table Expression). This attempts to exclusively lock the exact seats requested. If another transaction holds the lock, the current transaction waits. Once it acquires the lock, it verifies the seats are still available. If they are, it updates the `status` to `confirmed`. 
- **Why it's race-free:** PostgreSQL guarantees that `FOR UPDATE` row locks prevent any other transaction from concurrently updating or locking those exact rows until the current transaction commits or rolls back. 
- **Avoiding deadlocks (multi-seat):** If Transaction A locks seat `A1` and tries to lock `A2`, while Transaction B locks `A2` and tries to lock `A1`, a deadlock occurs. To mathematically prevent this, the `SELECT ... FOR UPDATE` query explicitly includes `ORDER BY name`. This guarantees all concurrent transactions lock the shared resources in the exact same deterministic order, eliminating the possibility of deadlocks.
- **Per-user limit:** We use `pg_advisory_xact_lock(hash(user_id), hash(show_id))` to take a transaction-level lock on the specific user+show combination. This forces concurrent requests from the *same* user to execute sequentially, preventing them from bypassing the limit via a read-modify-write race condition, while requests from *other* users proceed in parallel.

## Idempotency
- **Storage:** The `idempotency_key` is stored as a column in the `reservations` table.
- **Enforcement:** There is a `UNIQUE (user_id, idempotency_key)` constraint on the `reservations` table.
- **Exactly-once:** When a request arrives, we first check if the `(user_id, idempotency_key)` combination already exists in the `reservations` table. If it does, we short-circuit and return the stored reservation details. If it doesn't, we proceed with the reservation and insert the record. Because the entire process (reserving seats and inserting the reservation) happens in one atomic transaction, we never double-charge.
- **Same-key, different-body:** When an existing reservation is found via the idempotency key, we check if the requested seats in the new payload match the `requested_seats` stored in the existing reservation. If they differ, we return a `409 Conflict` (Idempotency key used with different payload).

## Holds & Expiry (Release Model)
We opted for the **explicit cancel model**. 
- Users can hit `POST /reservations/{id}/cancel` to release their seats. 
- The cancellation happens inside a transaction. It locks the `reservations` row using `FOR UPDATE`, checks if the user owns it, and if it's not already cancelled, updates the reservation status to `cancelled` and the associated seats to `available`.
- This strictly guarantees that a release never resurrects a seat confirmed to someone else, as the seat's `reservation_id` must match the cancelled reservation.

## Consistency vs Availability (CAP)
In the event of a network partition (e.g., between the application and the database), this system prioritizes **Consistency (CP)** over Availability. 
A seat reservation system is essentially a financial ledger; we absolutely cannot afford to double-sell a seat. If the database is unreachable or partitioned, the `/health` endpoint fails closed (returning 503), and new reservation requests will fail. It is better to turn away buyers temporarily than to sell the same seat to two different people and deal with the fallout at the venue.

## Observability (2am Pager Alerts)
The application exposes Prometheus metrics at `/metrics`. I would set up PagerDuty alerts for:
1. **High 5xx Error Rate:** Any HTTP 5xx error implies an unhandled exception or database failure. The correctness bar dictates that all declines should be 4xx. A spike in 5xx means the system is crashing.
2. **Database Connection Errors / Readiness Probe Failures:** If the app can't talk to the database, the system is down.
3. **Reconciliation Invariant Breach:** A cron job or Prometheus rule should periodically check if `available + held + confirmed != total_seats`. If this is ever true, the atomic decision logic has fundamentally failed and data corruption has occurred.

## Zero 5xx & Load Shedding
Handling 20,000 perfectly concurrent connections creates immense pressure on the database connection pool (which is limited to `80` to protect PostgreSQL). Instead of returning a `500 Server Error` when the pool queue times out, the API implements a global `asyncio.TimeoutError` exception handler. 
If a request cannot acquire a database connection in time, the API immediately intercepts the failure and returns a `429 Too Many Requests` (Domain Outcome). This strictly adheres to the "Zero 5xx" requirement and keeps the service highly available and uncorrupted under extreme stampedes.

## AI Usage
- **Directed:** I directed the AI to use PostgreSQL `FOR UPDATE ORDER BY` to handle the concurrency and deadlocks natively in the database.
- **Directed:** I directed the AI to use `pg_advisory_xact_lock` for the per-user limit to prevent write skew without resorting to serializable isolation levels or table locks.
- **Decided:** The AI decided on the exact layout of the FastAPI application, the Prometheus metric labels, and wrote the boilerplate `docker-compose` and `Dockerfile` configurations.

## What's Next
1. **Connection Pooling:** For production scale, I would add `PgBouncer` in front of the PostgreSQL database to handle thousands of concurrent TCP connections efficiently.
2. **Time-Boxed Holds:** I would add a cron worker or a delayed queue (like Celery/RabbitMQ) to support time-boxed holds (e.g., "you have 10 minutes to complete checkout") which automatically releases seats if payment isn't confirmed.
3. **Partitioning:** For a truly global scale (millions of users), I'd partition the `seats` and `reservations` tables by `show_id`.
