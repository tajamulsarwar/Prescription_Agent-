# Use Python 3.11 slim image (includes ODBC Driver 18 for SQL Server fallback)
FROM python:3.11-slim-bullseye

LABEL name="prescription_agent"

# Set working directory
WORKDIR /app

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LIVEKIT_LOG_LEVEL=INFO \
    LIVEKIT_LOG_FORMAT=json \
    DEBIAN_FRONTEND=noninteractive

# Install minimal system dependencies + Microsoft ODBC Driver 18 for SQL Server
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        curl \
        gcc \
        g++ \
        gnupg2 \
        unixodbc-dev; \
    curl -fsSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg; \
    echo "deb [arch=amd64 signed-by=/usr/share/keyrings/microsoft-prod.gpg] https://packages.microsoft.com/debian/11/prod bullseye main" \
        > /etc/apt/sources.list.d/mssql-release.list; \
    apt-get update; \
    ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18; \
    rm -rf /var/lib/apt/lists/*

# Copy requirements file
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Copy project files
COPY . .

# Ensure critical JSON configuration files are present
RUN ls -la dispatch-rule.json inbound-trunk.json || (echo "ERROR: Critical JSON config files missing!" && exit 1)

# Create non-root user for security
RUN useradd --create-home --shell /bin/bash appuser \
    && chown -R appuser:appuser /app
USER appuser

# Expose ports
# 5001 for Flask server
# 8080 for LiveKit agent port
EXPOSE 5001 8080

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import requests; requests.get('http://localhost:5001/getToken?name=healthcheck&room=test', timeout=5)" || exit 1

# Default command - can be overridden in docker-compose
CMD ["python", "agent.py", "dev"]
