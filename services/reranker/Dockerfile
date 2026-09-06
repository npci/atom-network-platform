# Cross-encoder reranker sidecar.
#
# Holds torch + sentence-transformers so the BACKEND image does not. See
# requirements.txt in this directory for the SBOM rationale (findings 2, 12,
# 13, 14, 17, 18) and services/reranker/README.md for how to deploy it.

FROM python:3.12-slim

WORKDIR /app

# Model cache location. Baked as an ENV so it is the same path at build time
# (if you choose to pre-bake weights, see the commented block below) and at
# run time, and so it can be mounted as a volume to survive container
# recreation — otherwise every restart re-downloads ~600 MB.
ENV HF_HOME=/models \
    SENTENCE_TRANSFORMERS_HOME=/models \
    # Bound the weight download. Without a timeout an HF stall hangs startup
    # indefinitely with no diagnostic; the in-process implementation hit exactly
    # this and needed a 45s thread deadline to work around it.
    HF_HUB_DOWNLOAD_TIMEOUT=20 \
    PYTHONUNBUFFERED=1

COPY requirements.txt .

# --timeout/--retries: the torch CPU wheel is ~190 MB and sentence-transformers
# drags a large transitive set. A single mirror blip should not burn the build.
RUN pip install --no-cache-dir --timeout 600 --retries 10 -r requirements.txt

COPY app ./app

# OPTIONAL — pre-bake the weights into the image instead of downloading on
# first boot. Uncomment for air-gapped deployments, where there is no internet
# at run time and a lazy download would mean the reranker never works at all.
# Costs ~600 MB of image size and requires network AT BUILD TIME.
#
# ARG PREFETCH_MODEL=false
# RUN if [ "$PREFETCH_MODEL" = "true" ]; then \
#       python -c "from sentence_transformers import CrossEncoder; \
#                  import os; CrossEncoder(os.getenv('RERANKER_MODEL', \
#                  'BAAI/bge-reranker-v2-m3'))"; \
#     fi

# Non-root, matching the convention in backend/Dockerfile. /models must
# be writable by this user or the first-boot weight download fails with a
# permission error that looks like a network problem.
RUN useradd --create-home --uid 10001 reranker \
    && mkdir -p /models \
    && chown -R reranker:reranker /models /app
USER reranker

EXPOSE 8200

# Single worker ON PURPOSE. Each worker loads its own ~600 MB copy of the model
# into memory, so `--workers 4` quadruples RAM for no throughput gain — the
# cross-encoder is CPU-bound and torch already parallelises internally across
# cores. Scale by running more CONTAINERS behind a load balancer, not more
# workers in one container.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8200", "--workers", "1"]
