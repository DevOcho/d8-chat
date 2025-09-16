#!/bin/sh

# This script is the entrypoint for the Docker container.
# It waits for the database to be ready, then initializes it,
# and finally executes the main command (gunicorn).

# Fail on any error
set -e

# Wait for the PostgreSQL database to be ready
echo "Waiting for PostgreSQL to be ready..."
while ! pg_isready -h db -p 5432 -q -U "$POSTGRES_USER"; do
  sleep 2
done
echo "PostgreSQL is ready."

# Run the database initialization script
# This will create tables and seed the initial admin user.
echo "Running database initialization..."
python init_db.py
echo "Database initialization complete."

# Execute the command passed to this script (the CMD from the Dockerfile)
exec "$@"
