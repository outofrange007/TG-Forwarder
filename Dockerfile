FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_PATH=/app/data \
    WEB_PORT=5000

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY forwarder ./forwarder

VOLUME ["/app/data"]
EXPOSE 5000

# Default: web dashboard. Alternatively e.g. "python -m forwarder run --live"
CMD ["python", "-m", "forwarder", "web"]
