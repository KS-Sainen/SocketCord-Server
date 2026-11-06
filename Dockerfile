# SocketCord backend image: python -m socketcord, listening on TCP 8080.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY socketcord ./socketcord
COPY client ./client

RUN useradd --system --uid 10001 socketcord
USER socketcord

EXPOSE 8080

# Healthy = the server accepts TCP connections.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import socket; socket.create_connection(('127.0.0.1', 8080), 2).close()" || exit 1

CMD ["python", "-m", "socketcord"]
