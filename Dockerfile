FROM python:3.11-slim

WORKDIR /app

ENV PYTHONPATH=/app/src
ENV PYTHONUNBUFFERED=1

COPY pyproject.toml README.md ./
COPY src ./src

EXPOSE 8000

CMD ["sh", "-c", "python -m investor_packet.qbo_callback --host 0.0.0.0 --port ${PORT:-8000}"]
