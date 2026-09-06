# Custom Postgres image combining pgvector + Apache AGE (Slice 19).
#
# Base is the official pgvector/pgvector:pg16 image (Postgres 16 with pgvector
# already compiled + installed). We add Apache AGE from source, matching the
# PG16 branch.
#
# Build: docker compose build postgres
# Run:   docker compose up -d postgres
#
# Data volume (`postgres_data`) survives this image change because the on-disk
# format is the same Postgres 16 cluster — only the extension binaries change.
# Existing databases work unchanged; AGE becomes available for `CREATE
# EXTENSION age` (done by alembic migration 0020).

FROM pgvector/pgvector:pg16

USER root

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        postgresql-server-dev-16 \
        git \
        ca-certificates \
        bison \
        flex \
    && rm -rf /var/lib/apt/lists/*

# Build + install Apache AGE, pinned to the PG16 branch.
RUN git clone --depth 1 --branch PG16 https://github.com/apache/age.git /tmp/age \
    && cd /tmp/age \
    && make \
    && make install \
    && rm -rf /tmp/age

# Drop build dependencies to keep the runtime image smaller.
RUN apt-get purge -y --auto-remove \
        build-essential \
        postgresql-server-dev-16 \
        git \
        bison \
        flex \
    && rm -rf /var/lib/apt/lists/*

USER postgres
