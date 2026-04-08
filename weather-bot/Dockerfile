FROM python:3.12-slim

WORKDIR /app

# System deps for scipy/numpy
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc g++ && \
    rm -rf /var/lib/apt/lists/*

# Install Node.js (for Strategy #1 copy-trader bot)
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl && \
    curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && \
    apt-get install -y nodejs && \
    rm -rf /var/lib/apt/lists/*

# Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy Strategy #1 (TS bot) and build
COPY polymarket/package*.json polymarket/
RUN cd polymarket && npm ci --production=false 2>/dev/null || true

COPY polymarket/ polymarket/
RUN cd polymarket && npx tsc 2>/dev/null || echo "TS build skipped (S1 may be disabled)"

# Copy Strategy #2 (Python weather bot)
COPY *.py ./
COPY cities.py config.py ./

# Create data directories
RUN mkdir -p data cache results logs

# Non-root user
RUN useradd -m botuser && chown -R botuser:botuser /app
USER botuser

ENTRYPOINT ["python", "main.py"]
