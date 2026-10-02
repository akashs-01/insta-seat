FROM python:3.11-slim

WORKDIR /app

# Install curl — used by docker-compose healthcheck (curl -f http://localhost:8000/readyz)
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Single uvicorn process: all async coroutines share one event loop and one asyncpg
# connection pool, which is the correct model for a high-concurrency async workload.
# For production horizontal scale-out: run multiple replicas of this container behind
# a load balancer — NOT multiple workers per container.
# The DB-level row locks and advisory locks work correctly across all replicas.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
