FROM python:3.12-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy package definition first for layer caching
COPY pyproject.toml .
COPY src/ src/

# Install package + live extras
RUN pip install --no-cache-dir -e ".[live]"

# Copy scripts last (changes more often than src/)
COPY scripts/ scripts/

EXPOSE 8080

ENV PYTHONUNBUFFERED=1 \
    LOG_LEVEL=INFO \
    EXECUTION_MODE=rh_approval \
    HTTP_PORT=8080

CMD ["python", "scripts/run_live.py"]
