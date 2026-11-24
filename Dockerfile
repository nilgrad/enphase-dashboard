# Use a slim Python image
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV FLASK_APP=server.app:create_app

# Install pinned dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application code
COPY . .

# Create a non-root user and ensure writable instance directory
RUN groupadd --system appgroup && useradd --system --gid appgroup --home /app appuser \
    && mkdir -p /app/instance \
    && chown -R appuser:appgroup /app

USER appuser

# Expose the port Flask runs on
EXPOSE 5000

# Container healthcheck against /api/health
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/api/health', timeout=3)"]

# Run the application using a production WSGI server (Gunicorn)
# Limit to 1 worker to prevent duplicate APScheduler instances; use threads for concurrency
CMD ["gunicorn", "--workers", "1", "--threads", "4", "--timeout", "60", "--bind", "0.0.0.0:5000", "server.app:create_app()"]
