import asyncio
import aiohttp
import time
import uuid
import os
import random

BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000")

async def create_show(session):
    seats = [f"A{i}" for i in range(1, 101)] # 100 seats
    payload = {
        "name": "friday-night",
        "seats": seats,
        "price_paise": 25000
    }
    async with session.post(f"{BASE_URL}/shows", json=payload) as resp:
        resp.raise_for_status()
        data = await resp.json()
        print(f"Created show: {data['id']} with {data['total_seats']} seats")
        return data['id']

async def reserve_seat_with_sem(sem, session, show_id, user_id, seat, idempotency_key):
    async with sem:
        headers = {
            "Authorization": f"Bearer {user_id}",
            "idempotency-key": idempotency_key
        }
        payload = {"seats": [seat]}
        try:
            async with session.post(f"{BASE_URL}/shows/{show_id}/reserve", json=payload, headers=headers) as resp:
                return resp.status
        except Exception as e:
            return 500

async def wait_for_api(session, retries=15, delay=2):
    """Poll /readyz until the API and its DB are both ready."""
    for attempt in range(retries):
        try:
            async with session.get(f"{BASE_URL}/readyz", timeout=aiohttp.ClientTimeout(total=3)) as resp:
                if resp.status == 200:
                    print(f"✅ API is ready (attempt {attempt + 1})")
                    return
        except Exception:
            pass
        print(f"⏳ Waiting for API... (attempt {attempt + 1}/{retries})")
        await asyncio.sleep(delay)
    raise RuntimeError("❌ API did not become ready in time. Is it running?")

async def main():
    connector = aiohttp.TCPConnector(limit=5000) # Bump aiohttp connection limit
    async with aiohttp.ClientSession(connector=connector) as session:
        await wait_for_api(session)
        show_id = await create_show(session)
        
        # Test 1: Stampede on 75 seats (3 hot, 72 normal)
        print("\n--- Test 1: Stampede on 75 seats (20,000 requests) ---")
        hot_seats = ["A26", "A27", "A28"]
        normal_seats = [f"A{i}" for i in range(29, 101)] # 72 seats
        tasks = []
        
        # Use a semaphore to prevent OS-level "Too many open files" errors on the client side 
        # while still heavily stressing the server with thousands of concurrent requests
        sem = asyncio.Semaphore(1000) 
        
        # 20,000 different users trying to book 75 seats at the same time
        for i in range(20000):
            user_id = f"user_{i}"
            idem_key = str(uuid.uuid4())
            # 50% chance to hit a hot seat, 50% chance to hit a normal seat
            if random.random() < 0.5:
                seat = random.choice(hot_seats)
            else:
                seat = random.choice(normal_seats)

            tasks.append(reserve_seat_with_sem(sem, session, show_id, user_id, seat, idem_key))
            
        start = time.time()
        results = await asyncio.gather(*tasks)
        print(f"Time taken: {time.time() - start:.2f}s")
        
        counts = {}
        for r in results:
            counts[r] = counts.get(r, 0) + 1

        print(f"Full distribution: { {k: v for k, v in sorted(counts.items())} }")
        print(f"Results for stampede: 201: {counts.get(201,0)}, 409: {counts.get(409,0)}, 429: {counts.get(429,0)}, 500: {counts.get(500,0)}")
        assert counts.get(201, 0) == 75, f"Expected exactly 75 201 winners, got: {counts}"
        assert counts.get(500, 0) == 0, f"Expected zero 500 errors, got: {counts}"

        # Test 2: Per-user limit (burst 10 requests for 10 different seats from same user)
        print("\n--- Test 2: Per-user limit (limit 4) ---")
        user_id = "greedy_user"
        tasks = []
        for i in range(1, 11):
            seat = f"A{i}" # A1 to A10
            idem_key = str(uuid.uuid4())
            tasks.append(reserve_seat_with_sem(sem, session, show_id, user_id, seat, idem_key))
            
        results = await asyncio.gather(*tasks)
        counts = {201: 0, 409: 0, 500: 0}
        for r in results:
            counts[r] = counts.get(r, 0) + 1
            
        print(f"Results for greedy_user: 201: {counts[201]}, 409: {counts[409]}, 500: {counts[500]}")
        assert counts[201] <= 4, "User bypassed limit"

        # Check final reconciliation
        async with session.get(f"{BASE_URL}/shows/{show_id}") as resp:
            data = await resp.json()
            print("\nFinal Reconciliation:")
            print(data['counts'])
            total = sum(data['counts'].values())
            print(f"Total counted: {total}, Expected: {data['total_seats']}")
            assert total == data['total_seats']

if __name__ == "__main__":
    asyncio.run(main())
