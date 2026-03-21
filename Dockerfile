# ── Kraken BTC Accumulation Bot ──────────────────────────────────────────────
# Python 3.12 slim image — small footprint, good performance
FROM python:3.12-slim

# Set working directory inside the container
WORKDIR /app

# Install dependencies first (cached layer if requirements.txt unchanged)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy bot source code
COPY bot/ ./bot/

# Create data and log directories
RUN mkdir -p /app/data /app/logs

# Run the bot
CMD ["python", "bot/main.py"]
