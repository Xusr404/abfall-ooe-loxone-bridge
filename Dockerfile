FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Europe/Vienna

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 --create-home app

WORKDIR /app

COPY bridge/requirements.txt /app/bridge/requirements.txt
RUN python -m pip install --no-cache-dir -r /app/bridge/requirements.txt

COPY bridge/ /app/bridge/
COPY tools/helpers/ /app/tools/helpers/

RUN mkdir -p /app/bridge/data /app/tools/helpers/data \
    && chown -R app:app /app

USER app

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "from urllib.request import urlopen; urlopen('http://127.0.0.1:8765/health', timeout=3)" || exit 1

CMD ["python", "/app/bridge/loxone_bridge.py", "--host", "0.0.0.0", "--port", "8765"]
