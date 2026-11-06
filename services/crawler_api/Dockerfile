ARG PYTHON_IMAGE=docker.m.daocloud.io/library/python@sha256:4840d514c583a4d3d8cad3940e68dee6f0f6f3635c12fcd0a3d74c21a5b93a54
FROM ${PYTHON_IMAGE} AS builder

ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/

WORKDIR /build
COPY uv.lock ./uv.lock
COPY contracts ./contracts
COPY services/crawler_api ./services/crawler_api
RUN python -c 'import pathlib, tomllib; lock = tomllib.loads(pathlib.Path("uv.lock").read_text()); pathlib.Path("constraints.txt").write_text("".join("{}=={}\n".format(p["name"], p["version"]) for p in lock["package"] if "registry" in p["source"]))' \
    && python -m pip install --no-cache-dir --index-url "${PIP_INDEX_URL}" \
    --constraint constraints.txt --prefix=/install ./contracts ./services/crawler_api

FROM ${PYTHON_IMAGE}

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

