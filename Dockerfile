# File: Dockerfile
# Dockerfile for the Flask app (app.py)
# Build:
#   docker build -t pysandbox-app .
# Run:
#   docker run -p 5000:5000 -v $(pwd)/workspace:/app/workspace pysandbox-app

FROM python:3.10-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install system deps (minimal)
RUN apt-get update && apt-get install -y --no-install-recommends build-essential curl ca-certificates && rm -rf /var/lib/apt/lists/*

# Copy application
COPY app.py /app/app.py
# Create workspace folder
RUN mkdir -p /app/workspace
VOLUME ["/app/workspace"]

# Install Python deps
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

EXPOSE 5000

CMD ["python", "app.py"]