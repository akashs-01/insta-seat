import uuid
import time
import asyncio
import json
import logging

import structlog
from fastapi import FastAPI, HTTPException, Header, Depends, status, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel
from typing import List
from contextlib import asynccontextmanager
from prometheus_client import Counter, Gauge, generate_latest
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

import database

# ---------------------------------------------------------------------------
# Structlog setup — structured JSON logs with correlation IDs
# ---------------------------------------------------------------------------
structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
)
logger = structlog.get_logger()
logging.getLogger("uvicorn.access").disabled = True

# ---------------------------------------------------------------------------
# Prometheus Metrics
# ---------------------------------------------------------------------------
RESERVATIONS_CONFIRMED = Counter('reservations_confirmed_total', 'Total confirmed reservations')
RESERVATIONS_DECLINED  = Counter('reservations_declined_total', 'Total declined reservations', ['reason'])
SEATS_AVAILABLE        = Gauge('seats_available', 'Seats currently available', ['show_id'])

# ---------------------------------------------------------------------------
# Rate Limiter (slowapi) — per-IP, 2000 req/min to block abuse
# ---------------------------------------------------------------------------
limiter = Limiter(key_func=get_remote_address)

# ---------------------------------------------------------------------------
# App lifespan
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    await database.init_db()
    yield
    await database.close_db()

app = FastAPI(lifespan=lifespan, title="Seat Reservation API")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# ---------------------------------------------------------------------------
# Request logging middleware — injects correlation_id into every log
# ---------------------------------------------------------------------------
@app.middleware("http")
async def structured_log_middleware(request: Request, call_next):
    correlation_id = str(uuid.uuid4())
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(
        correlation_id=correlation_id,
        method=request.method,
        path=request.url.path,
    )
    start = time.time()
    try:
        response = await call_next(request)
        status_code = response.status_code
    except Exception as e:
        status_code = 500
        raise e
    finally:
        logger.info("request", status_code=status_code, duration_ms=round((time.time() - start) * 1000, 2))
    return response

# ---------------------------------------------------------------------------
# Pydantic Models
# ---------------------------------------------------------------------------
class CreateShowRequest(BaseModel):
    name: str
    seats: List[str]
    price_paise: int

class ReserveRequest(BaseModel):
    seats: List[str]
    idempotency_key: str | None = None

# ---------------------------------------------------------------------------
# Auth — token IS the user_id (mock; swap for JWT verification in production)
# ---------------------------------------------------------------------------
def get_user_id(authorization: str = Header(...)):
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid token format")
    return authorization.split(" ")[1]

# ---------------------------------------------------------------------------
# Custom domain exception (triggers rollback + 409)
# ---------------------------------------------------------------------------
class ConflictError(Exception):
    def __init__(self, reason: str):
        self.reason = reason

@app.exception_handler(ConflictError)
async def conflict_error_handler(request: Request, exc: ConflictError):
    RESERVATIONS_DECLINED.labels(reason=exc.reason).inc()
    return JSONResponse(status_code=409, content={"detail": exc.reason})

@app.exception_handler(asyncio.TimeoutError)
async def timeout_error_handler(request: Request, exc: asyncio.TimeoutError):
    RESERVATIONS_DECLINED.labels(reason="system-overload").inc()
    return JSONResponse(status_code=429, content={"detail": "Service overloaded, please retry."})

@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    logger.error("unhandled_exception", error=str(exc))
    RESERVATIONS_DECLINED.labels(reason="system-overload").inc()
    return JSONResponse(status_code=429, content={"detail": "Service overloaded, please retry."})

# ---------------------------------------------------------------------------
# Health — separated into /livez (process alive) and /readyz (DB reachable)
# ---------------------------------------------------------------------------
@app.get("/livez", tags=["Health"])
async def liveness():
    """Liveness probe: just checks if the app process is running."""
    return {"status": "alive"}

@app.get("/readyz", tags=["Health"])
async def readiness():
    """Readiness probe: checks if the DB is reachable. Fails closed (503) if not."""
    try:
        async with database.pool.acquire() as conn:
            await conn.execute("SELECT 1")
        return {"status": "ready"}
    except Exception as e:
        logger.error("readiness_check_failed", error=str(e))
        return JSONResponse(status_code=503, content={"status": "unavailable", "detail": str(e)})

# Backwards-compatible alias
@app.get("/health", tags=["Health"])
async def health():
    return await readiness()

# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------
@app.get("/metrics", tags=["Observability"])
async def metrics():
    return PlainTextResponse(generate_latest())

# ---------------------------------------------------------------------------
# SHOWS
# ---------------------------------------------------------------------------
@app.post("/shows", status_code=status.HTTP_201_CREATED, tags=["Shows"])
async def create_show(request: Request, req: CreateShowRequest):
    show_id = str(uuid.uuid4())
    async with database.pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO shows (id, name, price_paise, total_seats) VALUES ($1, $2, $3, $4)",
                show_id, req.name, req.price_paise, len(req.seats)
            )
            records = [(show_id, seat_name) for seat_name in req.seats]
            await conn.copy_records_to_table('seats', columns=['show_id', 'name'], records=records)

    SEATS_AVAILABLE.labels(show_id=show_id).set(len(req.seats))
    logger.info("show_created", show_id=show_id, total_seats=len(req.seats))
    return {"id": show_id, "name": req.name, "price_paise": req.price_paise, "total_seats": len(req.seats)}

@app.get("/shows/{show_id}", tags=["Shows"])
async def get_show(show_id: str):
    async with database.pool.acquire() as conn:
        show = await conn.fetchrow("SELECT * FROM shows WHERE id = $1", show_id)
        if not show:
            raise HTTPException(status_code=404, detail="Show not found")
        counts = await conn.fetch(
            "SELECT status, count(*) FROM seats WHERE show_id = $1 GROUP BY status", show_id
        )
        status_counts = {"available": 0, "held": 0, "confirmed": 0}
        for row in counts:
            status_counts[row['status']] = row['count']

    return {
        "id": show['id'],
        "name": show['name'],
        "price_paise": show['price_paise'],
        "total_seats": show['total_seats'],
        "counts": status_counts
    }

# ---------------------------------------------------------------------------
# RESERVE
# ---------------------------------------------------------------------------
@app.post("/shows/{show_id}/reserve", status_code=status.HTTP_201_CREATED, tags=["Reservations"])
async def reserve_seats(
    show_id: str,
    req: ReserveRequest,
    request: Request,
    user_id: str = Depends(get_user_id)
):
    idempotency_key = req.idempotency_key or request.headers.get("idempotency-key")
    if not idempotency_key:
        raise HTTPException(status_code=400, detail="Idempotency key is required")

    async with database.pool.acquire() as conn:
        async with conn.transaction():

            # 1. Idempotency check — short-circuit if already processed
            existing_res = await conn.fetchrow(
                "SELECT id, requested_seats, status, amount_paise FROM reservations "
                "WHERE user_id = $1 AND idempotency_key = $2",
                user_id, idempotency_key
            )
            if existing_res:
                if set(existing_res['requested_seats']) != set(req.seats):
                    RESERVATIONS_DECLINED.labels(reason="idempotent-replay-mismatch").inc()
                    return JSONResponse(status_code=409, content={"detail": "Idempotency key used with different payload"})
                logger.info("idempotent_replay", reservation_id=existing_res['id'])
                return {
                    "reservation_id": existing_res['id'],
                    "show_id": show_id, "user_id": user_id,
                    "seats": existing_res['requested_seats'],
                    "amount_paise": existing_res['amount_paise'],
                    "status": existing_res['status'],
                }

            # 2. Advisory lock — serializes concurrent requests from the same user+show.
            #    hashtext() returns int4. pg_advisory_xact_lock(int4, int4) is the correct
            #    two-argument signature. Casting to bigint was wrong (no such overload exists).
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1)::int, hashtext($2)::int)",
                user_id, show_id
            )

            # 3. Fetch show
            show = await conn.fetchrow("SELECT price_paise FROM shows WHERE id = $1", show_id)
            if not show:
                raise HTTPException(status_code=404, detail="Show not found")
            amount_paise = show['price_paise'] * len(req.seats)

            # 4. Per-user seat limit check (index idx_seats_show_user makes this fast)
            user_seats_count = await conn.fetchval(
                "SELECT count(*) FROM seats WHERE show_id = $1 AND user_id = $2 AND status != 'available'",
                show_id, user_id
            )
            if user_seats_count + len(req.seats) > 4:
                RESERVATIONS_DECLINED.labels(reason="per-user-limit").inc()
                return JSONResponse(status_code=409, content={"detail": "Per-user limit exceeded"})

            # 5. Atomic seat claim — ORDER BY prevents deadlocks across concurrent multi-seat requests.
            #    FOR UPDATE locks rows; WHERE status='available' ensures only one transaction wins.
            res_id = str(uuid.uuid4())
            update_query = """
            WITH to_reserve AS (
                SELECT name FROM seats
                WHERE show_id = $1 AND name = ANY($2) AND status = 'available'
                ORDER BY name
                FOR UPDATE
            )
            UPDATE seats
            SET status = 'confirmed', reservation_id = $3, user_id = $4
            WHERE show_id = $1 AND name IN (SELECT name FROM to_reserve)
            RETURNING name;
            """
            reserved_seats = await conn.fetch(update_query, show_id, req.seats, res_id, user_id)

            if len(reserved_seats) != len(req.seats):
                raise ConflictError("seat-taken")

            # 6. Persist reservation record
            await conn.execute(
                "INSERT INTO reservations (id, show_id, user_id, status, idempotency_key, amount_paise, requested_seats) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7)",
                res_id, show_id, user_id, 'confirmed', idempotency_key, amount_paise, req.seats
            )

    RESERVATIONS_CONFIRMED.inc()
    SEATS_AVAILABLE.labels(show_id=show_id).dec(len(req.seats))
    logger.info("reservation_confirmed", reservation_id=res_id, user_id=user_id, seats=req.seats)

    return {
        "reservation_id": res_id,
        "show_id": show_id,
        "user_id": user_id,
        "seats": req.seats,
        "amount_paise": amount_paise,
        "status": "confirmed",
    }

# ---------------------------------------------------------------------------
# CANCEL
# ---------------------------------------------------------------------------
@app.post("/reservations/{reservation_id}/cancel", status_code=status.HTTP_200_OK, tags=["Reservations"])
async def cancel_reservation(reservation_id: str, user_id: str = Depends(get_user_id)):
    async with database.pool.acquire() as conn:
        async with conn.transaction():
            res = await conn.fetchrow(
                "SELECT id, show_id, status, requested_seats FROM reservations "
                "WHERE id = $1 AND user_id = $2 FOR UPDATE",
                reservation_id, user_id
            )
            if not res:
                raise HTTPException(status_code=404, detail="Reservation not found or unauthorized")
            if res['status'] == 'cancelled':
                return {"status": "cancelled", "message": "Already cancelled"}

            await conn.execute(
                "UPDATE seats SET status = 'available', reservation_id = NULL, user_id = NULL "
                "WHERE reservation_id = $1",
                reservation_id
            )
            await conn.execute(
                "UPDATE reservations SET status = 'cancelled' WHERE id = $1",
                reservation_id
            )

    SEATS_AVAILABLE.labels(show_id=res['show_id']).inc(len(res['requested_seats']))
    logger.info("reservation_cancelled", reservation_id=reservation_id, user_id=user_id)
    return {"status": "cancelled", "reservation_id": reservation_id}
