# ── Kraken BTC Accumulation Bot ──────────────────────────────────────────────
# Python 3.12 slim image — small footprint, good performance
FROM python:3.12-slim

# Set working directory inside the container
WORKDIR /app

# Install dependencies first (cached layer if requirements.txt unchanged)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy bot source code and scripts
COPY bot/ ./bot/
COPY scripts/ ./scripts/
COPY entrypoint.sh ./entrypoint.sh

# Create data and log directories
RUN mkdir -p /app/data /app/logs && chmod +x /app/entrypoint.sh

# Run via entrypoint (seeds price history, then starts bot)
CMD ["/app/entrypoint.sh"]
