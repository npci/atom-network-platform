FROM python:3.12-slim

WORKDIR /app

# Unbuffered stdout/stderr. Python BLOCK-buffers stdout when it is a pipe (as it
# always is under Docker) but line-buffers stderr, so without this the INFO half
# of the console severity split (app/core/log_buffer.py) would reach
# `docker compose logs -f` in delayed ~4-8 KB bursts while warnings appeared
# immediately — looking like lag and out-of-order logging.
ENV PYTHONUNBUFFERED=1

# System dependencies for WeasyPrint, Playwright, psycopg2, git, docker CLI,
# plus docgen merge (Session 20+): tesseract-ocr (OCR fallback for scanned PDFs
# in app/rag/chunking.py), plantuml + graphviz (UML diagram rendering for the
# docgen pipeline), fonts-dejavu-core (Pillow diagram fallback).
#
# default-jdk-headless (was -jre-): the agentic verification phase compiles Java
# (`mvn compile` → javac), which a JRE does not provide. The JDK is the same
# OpenJDK 21 the JRE pulled, just with the compiler. It is the ONLY new apt
# dependency for the agentic build path; Maven below adds none.
RUN apt-get update && apt-get install -y \
    gcc \
    libpq-dev \
    libffi-dev \
    libcairo2 \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libgdk-pixbuf-xlib-2.0-0 \
    libxml2 \
    libxslt1.1 \
    git \
    curl \
    tesseract-ocr \
    default-jdk-headless \
    plantuml \
    graphviz \
    fonts-dejavu-core \
    ffmpeg \
    && curl -fsSL https://download.docker.com/linux/static/stable/$(uname -m)/docker-27.5.1.tgz \
       | tar xz --strip-components=1 -C /usr/local/bin docker/docker \
    && rm -rf /var/lib/apt/lists/*

# Maven (agentic build/verify, §9/§18). Installed as a PINNED, checksum-verified
# binary tarball rather than `apt-get install maven` — Maven is just a JDK app
# (shell launcher + jars), so this pulls NO apt dependency chain, is architecture
# independent (pure Java), and is fully reproducible across machines (the exact
# bytes are fixed by the sha512). Baked into the image → portable at runtime.
# `mvn -v` at the end fails the build loudly if it cannot actually run.
ENV MAVEN_VERSION=3.9.9 \
    JAVA_HOME=/usr/lib/jvm/default-java
RUN curl -fsSL -o /tmp/maven.tgz \
      "https://archive.apache.org/dist/maven/maven-3/${MAVEN_VERSION}/binaries/apache-maven-${MAVEN_VERSION}-bin.tar.gz" \
 && curl -fsSL -o /tmp/maven.sha512 \
      "https://archive.apache.org/dist/maven/maven-3/${MAVEN_VERSION}/binaries/apache-maven-${MAVEN_VERSION}-bin.tar.gz.sha512" \
 && echo "$(cat /tmp/maven.sha512)  /tmp/maven.tgz" | sha512sum -c - \
 && mkdir -p /opt/maven \
 && tar xzf /tmp/maven.tgz -C /opt/maven --strip-components=1 \
 && ln -sf /opt/maven/bin/mvn /usr/local/bin/mvn \
 && rm -f /tmp/maven.tgz /tmp/maven.sha512 \
 && mvn -v

# Eclipse JDT Language Server (jdtls) for the OPTIONAL agentic LSP diagnostics tool
# (AGENTIC_LSP_ENABLED). It's a ~1 GB JVM server, so it's OFF by default and NOT baked
# in unless you ask: high-RAM prod/dev hosts build with `--build-arg INSTALL_JDTLS=true`,
# small/local boxes leave it out (fast build, no bloat — the tool then degrades cleanly).
ARG INSTALL_JDTLS=false
RUN if [ "$INSTALL_JDTLS" = "true" ]; then \
      mkdir -p /opt/jdtls \
   && curl -fsSL -o /tmp/jdtls.tgz "https://download.eclipse.org/jdtls/snapshots/jdt-language-server-latest.tar.gz" \
   && tar xzf /tmp/jdtls.tgz -C /opt/jdtls \
   && rm -f /tmp/jdtls.tgz \
   && ls /opt/jdtls/plugins/org.eclipse.equinox.launcher_*.jar >/dev/null \
   && echo "jdtls installed"; \
    else echo "jdtls NOT installed (INSTALL_JDTLS=false) — LSP tool will degrade if enabled"; fi

# requirements.txt is copied for reference only — the INSTALL comes from the
# hash-locked closure below. Keep both so `pip freeze` can be diffed against the
# human-edited source.
COPY requirements.txt requirements.arm64.lock requirements.amd64.lock ./
# SEC-3 — install from the lock, never from requirements.txt. requirements.txt
# pins VERSIONS; the lock additionally pins CONTENT (SHA-256 per distribution)
# across the full transitive closure, so a substituted or re-uploaded artifact
# at a pinned version fails the build instead of shipping.
#
# ONE LOCK PER ARCH: torch 2.6.0 declares NVIDIA CUDA dependencies on amd64 that
# do not exist in the arm64 graph (189 packages vs 175), so no single file can
# satisfy both under --require-hashes. The filenames deliberately match the
# values BuildKit puts in TARGETARCH ("arm64" / "amd64") so there is no mapping
# table here to fall out of step with the files.
#
# Editing requirements.txt alone now has NO effect on the image — regenerate the
# locks (command in each lock's header). scripts/ci/hygiene-check.sh fails the
# commit when a pin in requirements.txt is missing from a lock, which is what
# stops that divergence reaching a build.
#
# Slow / flaky PyPI mirrors over docker's default network occasionally
# read-time-out on large wheels (e.g. torch CPU 200MB, sentence-transformers
# transitive deps). Boost the per-request retry budget and timeout so a
# single network blip doesn't burn the whole 30-min image build.
ARG TARGETARCH
RUN test -n "$TARGETARCH" || { \
        echo "ERROR: TARGETARCH is empty. This build requires BuildKit (docker buildx," >&2; \
        echo "       or DOCKER_BUILDKIT=1) — the legacy builder does not set it." >&2; \
        exit 1; }; \
    test -f "requirements.${TARGETARCH}.lock" || { \
        echo "ERROR: no lockfile for TARGETARCH=${TARGETARCH}. Generate" >&2; \
        echo "       requirements.${TARGETARCH}.lock the same way as the existing ones." >&2; \
        exit 1; }; \
    pip install --no-cache-dir \
        --timeout 600 \
        --retries 10 \
        --require-hashes \
        -r "requirements.${TARGETARCH}.lock"

# multilspy — installed SEPARATELY and with --no-deps, on purpose.
#
# multilspy 0.0.15 declares `requests==2.32.3` as an exact pin. Inside the lock
# that single line pinned the whole image to a requests with two advisories
# (CVE-2024-47081, CVE-2026-25645), and an `==` cannot be overridden by a floor.
# Upstream is abandoned — 0.0.15 is the latest release and dates from 2025-04-03
# — so the pin was never going to lift on its own.
#
# --no-deps installs the package without its dependency opinions. That is safe
# here because all four of its real dependencies are already in the hash-locked
# closure installed above (jedi-language-server, psutil, typing-extensions,
# requests), so nothing is missing at runtime — only the version veto is gone.
# multilspy uses exactly one requests API, `requests.get(url, stream=True,
# timeout=60)`, to fetch language-server archives; verified working against
# requests 2.34.2.
#
# Kept OUT of the lock rather than pinned in it because --require-hashes applies
# to the whole file: a hash-pinned multilspy would drag its `requests==2.32.3`
# metadata back into resolution and re-conflict with the requests 2.34.2 pin.
#
# FAILURE MODE IS SOFT, which is what makes this acceptable. Every use site
# lazy-imports multilspy behind a try/except: `is_multilspy_available()` in
# app/rag/lsp_resolver_common.py returns False on ANY exception, and the deep
# import at line ~308 records a failure and returns an empty result. So if this
# install is ever wrong, the LSP cross-file resolvers degrade to disabled — they
# do not crash the app. Verified: with dependencies absent, the probe returns
# False cleanly rather than raising.
#
# If --no-deps ever proves fragile, the heavier fallback is vendoring multilspy
# into the image the way jdtls is fetched above.
RUN pip install --no-cache-dir --no-deps multilspy==0.0.15 \
 && python -c "import multilspy; from multilspy.multilspy_config import MultilspyConfig; print('multilspy import OK')"

# Mermaid CLI (mmdc) for the OPTIONAL Mermaid diagram engine (DOCGEN_DIAGRAM_ENGINE=mermaid).
# OFF by default (mirrors INSTALL_JDTLS) — build `--build-arg INSTALL_MERMAID=true` to enable.
#
# PLAYWRIGHT NOW LIVES INSIDE THIS BLOCK (moved 2026-08-28, SBOM finding 5).
# It used to be an unconditional `pip` pin in requirements.txt plus an
# unconditional `RUN playwright install chromium` immediately above this
# comment. That was wrong on both counts:
#
#   1. NOTHING IN THE PYTHON TREE IMPORTS PLAYWRIGHT. Verified by grep — no
#      `from playwright`, no `import playwright`, no `sync_playwright`. The
#      requirements.txt comment claimed it was for "HTML to image (prototype
#      screens)" but no such code was ever written. Its only real job was to
#      DOWNLOAD A CHROMIUM BINARY that mermaid-cli borrows, five lines below.
#   2. So in the default build (INSTALL_MERMAID=false) the pip package AND a
#      ~150 MB browser were installed and then used by absolutely nothing —
#      while carrying CVE-2025-59288 (CVSS 8.7, Security-High) into every scan
#      of this image.
#
# WHY IT IS STILL PLAYWRIGHT AND NOT apt's chromium: Debian's arm64 chromium
# SIGTRAPs under Docker on Apple Silicon (verified the hard way), whereas
# Playwright's build runs on BOTH arm64 and x86. That is hard-won knowledge and
# is deliberately NOT being discarded here — the browser source is unchanged.
# The ONLY change is that it is now installed when its consumer is installed,
# instead of always. Mermaid rendering is therefore bit-for-bit what it was.
#
# Chromium is copied to a world-readable path so the non-root app user can exec
# it, and a puppeteer config supplies --no-sandbox (no seccomp change needed).
# When mmdc is absent the pipeline falls back to PlantUML/Pillow, so the default
# build stays lean. (Prod runs native — there the config's executablePath points
# at the host Chrome instead; see the offline-install notes.)
ARG INSTALL_MERMAID=false
ENV MERMAID_PUPPETEER_CONFIG=/etc/puppeteer-config.json
RUN if [ "$INSTALL_MERMAID" = "true" ]; then \
      apt-get update \
   && apt-get install -y --no-install-recommends nodejs npm fonts-liberation \
   && PUPPETEER_SKIP_DOWNLOAD=1 npm install -g @mermaid-js/mermaid-cli@11 \
   && pip install --no-cache-dir playwright==1.49.0 \
   && playwright install chromium \
   && PW="$(find /root/.cache/ms-playwright -type f -name headless_shell | head -1)" \
   && { test -n "$PW" || { echo "ERROR: playwright chromium not found after install" >&2; exit 1; }; } \
   && mkdir -p /opt/mmdc-chromium \
   && cp -r "$(dirname "$PW")" /opt/mmdc-chromium/ \
   && chmod -R a+rX /opt/mmdc-chromium \
   && printf '{"executablePath":"/opt/mmdc-chromium/chrome-linux/headless_shell","args":["--no-sandbox","--disable-gpu","--disable-dev-shm-usage"]}\n' > /etc/puppeteer-config.json \
   && chmod a+r /etc/puppeteer-config.json \
   && rm -rf /var/lib/apt/lists/* \
   && mmdc --version; \
    else echo "mermaid-cli NOT installed (INSTALL_MERMAID=false) — Mermaid engine falls back to PlantUML/Pillow"; fi

COPY . .

RUN adduser --disabled-password --no-create-home --gecos "" appuser \
 && chown -R appuser /app \
 # Give appuser a real home + writable Maven local repo. Without this, `mvn` runs as
 # appuser with HOME=/home/appuser (which doesn't exist) and aborts every build with
 # LocalRepositoryNotAccessibleException (can't create ~/.m2). Prod runs natively as a
 # real user with a real home, so this only matters for the containerised dev image.
 && mkdir -p /home/appuser/.m2 \
 && chown -R appuser /home/appuser
USER appuser

EXPOSE 8000
