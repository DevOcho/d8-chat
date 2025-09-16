#!/bin/sh

# This script is the entrypoint for the Docker container.
# It waits for dependencies to be ready, then initializes the DB,
# and finally executes the main command (gunicorn).

# Fail on any error
set -e

# Use POSTGRES_HOST env var, with a default of 'db' for docker-compose
POSTGRES_HOST=${POSTGRES_HOST:-db}
# Use MINIO_ENDPOINT env var, but strip the port for the health check
MINIO_HOST=$(echo "${MINIO_ENDPOINT:-minio:9000}" | cut -d: -f1)

# --- Wait for PostgreSQL ---
echo "Waiting for PostgreSQL to be ready at host: $POSTGRES_HOST..."
while ! pg_isready -h "$POSTGRES_HOST" -p 5432 -q -U "$POSTGRES_USER"; do
  echo "Postgres is unavailable - sleeping"
  sleep 2
done
echo "✅ PostgreSQL is ready."

# --- Wait for Minio ---
echo "Waiting for Minio to be ready at host: $MINIO_HOST..."
# The health check endpoint is at http://<minio-host>:9000/minio/health/live
while ! curl -s "http://$MINIO_HOST:9000/minio/health/live" > /dev/null; do
  echo "Minio is unavailable - sleeping"
  sleep 2
done
echo "✅ Minio is ready."

# Run the database initialization script
echo "Running database initialization..."
python init_db.py
echo "Database initialization complete."

# Execute the command passed to this script (the CMD from the Dockerfile)
echo "--- Starting Gunicorn ---"
exec "$@"
