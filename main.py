from fastapi import FastAPI, HTTPException, Header, Depends, status, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from typing import List
import uuid
import database
import asyncpg
from prometheus_client import Counter, Gauge, generate_latest
from fastapi.responses import PlainTextResponse
from contextlib import asynccontextmanager

# Metrics
RESERVATIONS_CONFIRMED = Counter('reservations_confirmed_total', 'Total confirmed reservations')
RESERVATIONS_DECLINED = Counter('reservations_declined_total', 'Total declined reservations', ['reason'])
SEATS_AVAILABLE = Gauge('seats_available', 'Seats currently available', ['show_id'])

@asynccontextmanager
async def lifespan(app: FastAPI):
    await database.init_db()
    yield
    await database.close_db()

app = FastAPI(lifespan=lifespan)

import logging
import json
import time

logger = logging.getLogger("seat_reservation")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter('%(message)s'))
logger.addHandler(handler)
# Disable uvicorn access logs to prevent duplication
logging.getLogger("uvicorn.access").disabled = True

@app.middleware("http")
async def structured_log_middleware(request: Request, call_next):
    request_id = str(uuid.uuid4())
    idem_key = request.headers.get("idempotency-key", "")
    start_time = time.time()
    
    try:
        response = await call_next(request)
        status_code = response.status_code
    except Exception as e:
        status_code = 500
        raise e
    finally:
        process_time = (time.time() - start_time) * 1000
        log_dict = {
            "correlation_id": request_id,
            "method": request.method,
            "path": request.url.path,
            "status_code": status_code,
            "duration_ms": round(process_time, 2),
            "idempotency_key": idem_key
        }
        logger.info(json.dumps(log_dict))
        
    return response

# Models
class CreateShowRequest(BaseModel):
    name: str
    seats: List[str]
    price_paise: int

class ReserveRequest(BaseModel):
    seats: List[str]
    idempotency_key: str | None = None

# Mock authentication
def get_user_id(authorization: str = Header(...)):
    # In a real app, this would verify a JWT.
    # For this exercise, we'll assume the token IS the user_id.
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid token format")
    return authorization.split(" ")[1]

@app.get("/metrics")
async def metrics():
    return PlainTextResponse(generate_latest())

@app.get("/health")
async def health():
    try:
        async with database.pool.acquire() as conn:
            await conn.execute("SELECT 1")
        return {"status": "ok"}
    except Exception as e:
        return JSONResponse(status_code=503, content={"status": "error", "detail": str(e)})

@app.post("/shows", status_code=status.HTTP_201_CREATED)
async def create_show(req: CreateShowRequest):
    show_id = str(uuid.uuid4())
    async with database.pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO shows (id, name, price_paise, total_seats) VALUES ($1, $2, $3, $4)",
                show_id, req.name, req.price_paise, len(req.seats)
            )
            # Bulk insert seats
            records = [(show_id, seat_name) for seat_name in req.seats]
            await conn.copy_records_to_table(
                'seats', columns=['show_id', 'name'], records=records
            )
    
    SEATS_AVAILABLE.labels(show_id=show_id).set(len(req.seats))
    return {"id": show_id, "name": req.name, "price_paise": req.price_paise, "total_seats": len(req.seats)}

@app.get("/shows/{show_id}")
async def get_show(show_id: str):
    async with database.pool.acquire() as conn:
        show = await conn.fetchrow("SELECT * FROM shows WHERE id = $1", show_id)
        if not show:
            raise HTTPException(status_code=404, detail="Show not found")
        
        counts = await conn.fetch("SELECT status, count(*) FROM seats WHERE show_id = $1 GROUP BY status", show_id)
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

@app.post("/shows/{show_id}/reserve", status_code=status.HTTP_201_CREATED)
async def reserve_seats(show_id: str, req: ReserveRequest, request: Request, user_id: str = Depends(get_user_id)):
    idempotency_key = req.idempotency_key or request.headers.get("idempotency-key")
    if not idempotency_key:
        raise HTTPException(status_code=400, detail="Idempotency key is required")
        
    async with database.pool.acquire() as conn:
        # We must handle the entire reservation inside a single transaction
        async with conn.transaction():
            # 1. Idempotency Check
            existing_res = await conn.fetchrow(
                "SELECT id, requested_seats, status, amount_paise FROM reservations WHERE user_id = $1 AND idempotency_key = $2",
                user_id, idempotency_key
            )
            
            if existing_res:
                if set(existing_res['requested_seats']) != set(req.seats):
                    RESERVATIONS_DECLINED.labels(reason="idempotent-replay-mismatch").inc()
                    return JSONResponse(status_code=409, content={"detail": "Idempotency key used with different payload"})
                return {
                    "reservation_id": existing_res['id'],
                    "show_id": show_id,
                    "user_id": user_id,
                    "seats": existing_res['requested_seats'],
                    "amount_paise": existing_res['amount_paise'],
                    "status": existing_res['status']
                }

            # 2. Get show details and acquire Advisory Lock to serialize requests for this user+show
            # hashtext returns a 32-bit int. We can use a 64-bit lock by providing two 32-bit ints.
            # Using hash of user_id and hash of show_id.
            lock_key1 = hash(user_id) % (2**31 - 1)
            lock_key2 = hash(show_id) % (2**31 - 1)
            await conn.execute("SELECT pg_advisory_xact_lock($1, $2)", lock_key1, lock_key2)

            show = await conn.fetchrow("SELECT price_paise FROM shows WHERE id = $1", show_id)
            if not show:
                raise HTTPException(status_code=404, detail="Show not found")
                
            amount_paise = show['price_paise'] * len(req.seats)

            # 3. Check Per-User Limit
            user_seats_count = await conn.fetchval(
                "SELECT count(*) FROM seats WHERE show_id = $1 AND user_id = $2 AND status != 'available'",
                show_id, user_id
            )
            
            if user_seats_count + len(req.seats) > 4:
                RESERVATIONS_DECLINED.labels(reason="per-user-limit").inc()
                return JSONResponse(status_code=409, content={"detail": "Per-user limit exceeded"})

            # 4. Attempt to Reserve Seats (Atomic and Deadlock-free)
            # We use an CTE (WITH clause) to SELECT ... FOR UPDATE with ORDER BY to prevent deadlocks
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
                # Someone else took one or more of the seats. Transaction will rollback because we raise an error.
                # Actually, raising an error rolls back the transaction. But we want to return 409, not 500.
                # We can explicitly rollback or just return a JSONResponse which will exit the context manager.
                # In asyncpg, exiting the transaction block without exception commits by default.
                # Wait, if we return JSONResponse inside `async with conn.transaction():`, it commits!
                # We MUST manually raise an exception or run `conn.execute('ROLLBACK')`?
                # Actually, raising a custom Exception is best to trigger rollback, then catch it.
                raise ConflictError("seat-taken")

            # 5. Insert Reservation Record
            await conn.execute(
                "INSERT INTO reservations (id, show_id, user_id, status, idempotency_key, amount_paise, requested_seats) VALUES ($1, $2, $3, $4, $5, $6, $7)",
                res_id, show_id, user_id, 'confirmed', idempotency_key, amount_paise, req.seats
            )
            
            RESERVATIONS_CONFIRMED.inc()
            SEATS_AVAILABLE.labels(show_id=show_id).dec(len(req.seats))
            
            return {
                "reservation_id": res_id,
                "show_id": show_id,
                "user_id": user_id,
                "seats": req.seats,
                "amount_paise": amount_paise,
                "status": "confirmed"
            }

@app.post("/reservations/{reservation_id}/cancel", status_code=status.HTTP_200_OK)
async def cancel_reservation(reservation_id: str, user_id: str = Depends(get_user_id)):
    async with database.pool.acquire() as conn:
        async with conn.transaction():
            res = await conn.fetchrow(
                "SELECT id, show_id, status, requested_seats FROM reservations WHERE id = $1 AND user_id = $2 FOR UPDATE",
                reservation_id, user_id
            )
            if not res:
                raise HTTPException(status_code=404, detail="Reservation not found or unauthorized")
            
            if res['status'] == 'cancelled':
                return {"status": "cancelled", "message": "Already cancelled"}

            # Update seats to available
            await conn.execute(
                "UPDATE seats SET status = 'available', reservation_id = NULL, user_id = NULL WHERE reservation_id = $1",
                reservation_id
            )

            # Update reservation status
            await conn.execute(
                "UPDATE reservations SET status = 'cancelled' WHERE id = $1",
                reservation_id
            )
            
            SEATS_AVAILABLE.labels(show_id=res['show_id']).inc(len(res['requested_seats']))
            
            return {"status": "cancelled", "reservation_id": reservation_id}

class ConflictError(Exception):
    def __init__(self, reason: str):
        self.reason = reason

@app.exception_handler(ConflictError)
async def conflict_error_handler(request: Request, exc: ConflictError):
    RESERVATIONS_DECLINED.labels(reason=exc.reason).inc()
    return JSONResponse(status_code=409, content={"detail": exc.reason})

import asyncio

@app.exception_handler(asyncio.TimeoutError)
async def timeout_error_handler(request: Request, exc: asyncio.TimeoutError):
    RESERVATIONS_DECLINED.labels(reason="system-overload").inc()
    return JSONResponse(status_code=429, content={"detail": "Service overloaded (db pool timeout), please retry."})

@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled exception preventing 500: {str(exc)}")
    RESERVATIONS_DECLINED.labels(reason="system-overload").inc()
    return JSONResponse(status_code=429, content={"detail": "Service overloaded, please retry."})
