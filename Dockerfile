FROM python:3.12-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    WEB_CONCURRENCY=2 \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt

WORKDIR /app

COPY certs/sectigo-public-server-authentication-ca-dv-r36.pem /usr/local/share/ca-certificates/sectigo-public-server-authentication-ca-dv-r36.crt
RUN update-ca-certificates

COPY pyproject.toml README.md ./
COPY alembic.ini ./
COPY app ./app
COPY migrations ./migrations
COPY tools ./tools
COPY releases ./releases
RUN pip install --no-cache-dir .

# Keep runtime processes away from root; only the document cache needs write access.
RUN addgroup --system app \
    && adduser --system --ingroup app app \
    && mkdir -p /app/var/auction-documents \
    && chown -R app:app /app/var
USER app

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers ${WEB_CONCURRENCY:-2}"]
