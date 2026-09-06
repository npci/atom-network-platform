# End-to-end verification scripts

These run the **real chain on real Postgres** — no SQLite, no in-memory table
subsets, foreign keys enforced. The unit suites deliberately build their own
SQLite engine per test, so they never exercise FK constraints, JSON column
semantics or enum handling as the production database does. These scripts do.

Neither script is a pytest test on purpose: they need a live database at
migration head and they write real rows, which is the point.

| Script | Proves |
|---|---|
| `cert_e2e_happy_path.py` | The full chain works: registry delta → baseline pack → publish → two certification rounds → reporting → pack diff → mode refusal. 26 checks. |
| `cert_e2e_defect_detection.py` | The chain **detects a planted defect**: a contract answering the wrong response code is caught by the grader, the round records `failed` not `certified`, and CERT-7 reports it under `newly_failing`. 9 checks. |

The second matters more than the first. A certification platform that only ever
passes proves nothing; these scripts show it both passes correctly and fails
correctly.

## Running them

```bash
cd /nirbhay/atom/atom-network-platform

# 1. A throwaway database on the compose network
docker run -d --name verify --network certagent_cert-net \
  -e POSTGRES_PASSWORD=v -e POSTGRES_DB=atom \
  atom-network-platform-postgres:latest
sleep 9
IP=$(docker inspect -f '{{(index .NetworkSettings.Networks "certagent_cert-net").IPAddress}}' verify)

# 2. Migrate to head (expect 0136)
CERTSIM_INTERNAL_TOKEN=dev POSTGRES_PASSWORD=dev REDIS_PASSWORD=dev \
docker compose run --rm --no-deps -v $PWD/backend:/app \
  -e DATABASE_URL="postgresql://postgres:v@${IP}:5432/atom" \
  -e SECRET_KEY=dev-test-secret-key-0123456789abcdef0123456789abcdef \
  backend alembic upgrade head

# 3. Happy path, then defect detection (order matters — the second builds on
#    the rows the first created)
for s in cert_e2e_happy_path cert_e2e_defect_detection; do
  CERTSIM_INTERNAL_TOKEN=dev POSTGRES_PASSWORD=dev REDIS_PASSWORD=dev \
  docker compose run --rm --no-deps -v $PWD/backend:/app \
    -e DATABASE_URL="postgresql://postgres:v@${IP}:5432/atom" \
    -e SECRET_KEY=dev-test-secret-key-0123456789abcdef0123456789abcdef \
    backend python scripts/verification/$s.py || echo "FAILED: $s"
done

# 4. Clean up
docker rm -f verify
```

Both scripts exit non-zero on any failed check, so they work in CI as-is.

## Note on what a green run proves

The pack's scenario, the simulator's answer and the grader's expectation all
derive from the same catalogue row — by design, since "both are projections of
the same rows" is what stops the simulator and the grader drifting apart. So a
green happy-path run proves the wiring, the message shape and the assertion
machinery. It does **not** prove any real bank behaves correctly. That is
exactly what the `evidence` sentence recorded on every run says in words.
