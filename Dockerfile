# Use an official Python runtime as a parent image
#FROM python:3.12-slim
FROM mirror.gcr.io/library/python:3.12-slim

# Set the working directory in the container
WORKDIR /app

# Copy just the requirements file first to leverage Docker layer caching
COPY requirements.txt .

# Install any needed packages specified in requirements.txt
# --no-cache-dir reduces image size
RUN pip install --no-cache-dir -r requirements.txt

# Install PostgreSQL client for pg_isready command
RUN apt-get update && apt-get install -y postgresql-client curl && rm -rf /var/lib/apt/lists/*

# Copy the rest of the application code into the container
COPY . .

# Make the entrypoint script executable
RUN chmod +x /app/entrypoint.sh

# Expose the port the app runs on
EXPOSE 5001

# Define the entrypoint script
ENTRYPOINT ["/app/entrypoint.sh"]

# The main command to run when the container starts.
# This will be passed as arguments to the entrypoint script.
#CMD ["gunicorn", "--worker-class", "gevent", "--workers", "4", "--bind", "0.0.0.0:5001", "run:app"]
CMD ["gunicorn", "--worker-class", "gevent", "--workers", "4", "--bind", "0.0.0.0:5001", "--access-logfile", "-", "--forwarded-allow-ips", "*", "run:app"]
