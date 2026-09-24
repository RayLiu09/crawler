FROM python:3.12-slim AS builder

WORKDIR /build
COPY contracts ./contracts
COPY services/crawler_api ./services/crawler_api
RUN python -m pip install --no-cache-dir --prefix=/install ./contracts ./services/crawler_api

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    API_HOST=0.0.0.0 \
    API_PORT=8080

RUN addgroup --system crawler && adduser --system --ingroup crawler crawler
WORKDIR /app
COPY --from=builder /install /usr/local
COPY services/crawler_api/alembic.ini ./alembic.ini
COPY services/crawler_api/src ./src

USER crawler
EXPOSE 8080
CMD ["python", "-m", "crawler_api.main"]

