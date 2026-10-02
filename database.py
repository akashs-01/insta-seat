import os
import asyncpg
from typing import List

# Default connection string, can be overridden by environment variable
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://user:password@localhost:5432/seat_reservation")

pool: asyncpg.Pool = None

async def init_db():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=20, max_size=80)
    
    async with pool.acquire() as conn:
        # Create tables
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS shows (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                price_paise INTEGER NOT NULL,
                total_seats INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS seats (
                show_id TEXT NOT NULL REFERENCES shows(id),
                name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'available', -- 'available', 'held', 'confirmed'
                reservation_id TEXT,
                user_id TEXT,
                PRIMARY KEY (show_id, name)
            );

            CREATE TABLE IF NOT EXISTS reservations (
                id TEXT PRIMARY KEY,
                show_id TEXT NOT NULL REFERENCES shows(id),
                user_id TEXT NOT NULL,
                status TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                amount_paise INTEGER NOT NULL,
                requested_seats TEXT[] NOT NULL,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                UNIQUE (user_id, idempotency_key)
            );
        """)

async def close_db():
    if pool:
        await pool.close()
