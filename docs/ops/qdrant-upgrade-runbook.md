# Qdrant 1.15 → 1.19 Upgrade Runbook (#1793)

From v0.87.0 the compose files pin `qdrant/qdrant:v1.19.1` and the backend
ships `qdrant-client` 1.18. A Qdrant volume created by an earlier release
(server 1.15) **cannot start on 1.19.1 directly**: it has to be upgraded one
minor version at a time. This runbook is that procedure for the single-server
stack (single-host and split-host layouts) and for a local development stack.

A routine `./scripts/deploy.sh` never recreates Qdrant (`--no-deps`), so
deploying the release does not by itself touch the volume. What does is any
command that recreates the `qdrant` service from the new compose files: the
data-tier recreate in [Container log rotation](../deployment.md#container-log-rotation),
a first `docker compose up -d` on a data VM, or `make up` on a dev machine.

## Why this is not an image-tag bump

Qdrant supports upgrading one minor version at a time
([upgrade instructions](https://qdrant.tech/documentation/faq/qdrant-fundamentals/#how-do-i-avoid-issues-when-updating-to-the-latest-version)).
Between 1.15 and 1.19 the storage changed underneath: 1.16 moves payload
indexes from RocksDB to Gridstore when it loads a collection, and 1.17 / 1.18
removed RocksDB support. A server that skips 1.16 never runs that migration.

Tested on 2026-10-02 against a volume this app created on 1.15.0 (the
`ensure_kagura_memories_collection` schema: dense + sparse vectors, all payload
indexes, 3,000 points):

| Path | Result |
|---|---|
| 1.15.0 → 1.19.1 directly | Panics while loading the shard: `unknown variant 'rocks_db', expected 'gridstore' or 'mmap'`. The container exits, so under `restart: always` it crash-loops and the API's `/readiness` reports Qdrant down |
| 1.15.0 → 1.16.3 → 1.17.1 → 1.18.3 → 1.19.1 | Every step loads; 1.16.3 logs `Migrating away from RocksDB indices for field …`. After each step the point count, every payload index and dense / sparse / tag / geo queries match the seeded data |
| The volume 1.19.1 failed on → 1.16.3 → … → 1.19.1 | Same as the stepwise row: the failed start left the volume usable |

### The client window

`qdrant-client` logs `Qdrant client version X is incompatible with server
version Y` when it is created (at API start, and at the start of every CLI
command) unless the majors match and the minors differ by at most one.

| Release | Client | Servers inside the window |
|---|---|---|
| v0.86.x and earlier | 1.16 | 1.15, 1.16, 1.17 |
| v0.87.0 | 1.18 | 1.17, 1.18, 1.19 |

The order below keeps every process start inside a window, so the warning
never appears. Outside it the client still works — on 2026-10-02 the 1.18
client created, filled and queried a collection on a 1.15.0 server without
errors — but each start logs the warning until the server reaches 1.17.

## Path

| Step | Qdrant image | API running |
|---|---|---|
| 1 | `v1.16.3` | your current release (≤ v0.86.x) |
| 2 | `v1.17.1` | your current release |
| 3 | — deploy v0.87.0 — | v0.87.0 |
| 4 | `v1.18.3` | v0.87.0 |
| 5 | `v1.19.1` (the image the compose files pin) | v0.87.0 |

Patch versions inside a minor may be skipped; the intermediate tags are the
latest patch of each minor as of 2026-10-02. A server already on 1.17 starts at
step 3, one on 1.16 at step 2.

## Before you start

- **Maintenance window.** Each step restarts Qdrant. While it loads, recall and
  any write that needs the vector store fail; the restart takes seconds on a
  small collection and grows with its size.
- **Back up the disk** — [Manual snapshot](../../terraform/single-server/README.md#manual-snapshot)
  on GCE, or stop Qdrant and copy the `kagura_qdrant_data` volume elsewhere.
- Run the commands below on the VM from `/opt/kagura-memory/src/terraform/single-server`.

## Procedure — single host

Two helpers. `qdrant_status` reads the server through a running API container
(the Qdrant image has no curl; the API container has the URL and the key in its
environment) and prints the version and each collection's status and point
count. The overlay swaps only the image, so the intermediate steps run from the
same service definition — network, volume, API key — as the real one.

```bash
qdrant_status() {
  docker exec -i "$(docker ps -q -f name=^kagura-api- | head -n1)" python - <<'PY'
import os, httpx
base, key = os.environ["QDRANT_URL"], os.environ.get("QDRANT_API_KEY") or ""
h = {"api-key": key} if key else {}
httpx.get(base + "/readyz", headers=h).raise_for_status()
print("version", httpx.get(base + "/", headers=h).json()["version"])
for c in httpx.get(base + "/collections", headers=h).json()["result"]["collections"]:
    r = httpx.get(f"{base}/collections/{c['name']}", headers=h).json()["result"]
    print(c["name"], r["status"], r["points_count"])
PY
}
wait_qdrant() {  # up to 5 minutes; a crash loop never gets ready
  for _ in $(seq 1 60); do qdrant_status 2>/dev/null && return 0; sleep 5; done
  echo "Qdrant did not become ready" >&2; docker logs --tail 30 kagura-qdrant; return 1
}
cat > /tmp/qdrant-step.yml <<'EOF'
services:
  qdrant:
    image: qdrant/qdrant:${QDRANT_STEP_TAG:?set QDRANT_STEP_TAG}
EOF

qdrant_status | tee /tmp/qdrant-before.txt
```

Steps 1–2, with your current API still serving:

```bash
for tag in v1.16.3 v1.17.1; do
  QDRANT_STEP_TAG=$tag docker compose -f docker-compose.prod.yml -f /tmp/qdrant-step.yml \
    --env-file .env.prod up -d --no-deps qdrant
  wait_qdrant || break
done
```

Step 3 — deploy v0.87.0 as usual ([Update to a new release](../../terraform/single-server/README.md#update-to-a-new-release-zero-downtime)):

```bash
cd /opt/kagura-memory/src && git fetch && git reset --hard origin/main
cd terraform/single-server && ./scripts/deploy.sh
```

Steps 4–5. The last one uses the compose file alone, which leaves the service
exactly as the release defines it:

```bash
QDRANT_STEP_TAG=v1.18.3 docker compose -f docker-compose.prod.yml -f /tmp/qdrant-step.yml \
  --env-file .env.prod up -d --no-deps qdrant && wait_qdrant \
&& docker compose -f docker-compose.prod.yml --env-file .env.prod up -d --no-deps qdrant \
&& wait_qdrant | tee /tmp/qdrant-after.txt
diff <(tail -n +2 /tmp/qdrant-before.txt) <(tail -n +2 /tmp/qdrant-after.txt) && echo "collections unchanged"
```

Every collection should read `green` with the point count it had before. A
count that moved because the API took writes during the window is expected;
one that dropped to zero is not — stop and restore the snapshot.

## Procedure — split host

Run the `up` commands on the **data VM** with the data-tier files, and the
helpers on the **app VM** (they need an API container):

```bash
# data VM — e.g. step 1 (DATA_BIND_ADDR as in README "Optional: split-host layout")
DATA_BIND_ADDR=192.168.10.20 QDRANT_STEP_TAG=v1.16.3 docker compose \
  -f docker-compose.data.yml -f docker-compose.data-expose.yml -f /tmp/qdrant-step.yml \
  --env-file .env.prod up -d --no-deps qdrant
```

The order of the steps and the deploy in step 3 (on the app VM) are the same.

## If a step does not come up

`docker logs --tail 50 kagura-qdrant` names the failure. A `rocks_db` panic
means a minor was skipped: go back to the step after the last one that loaded
(the failed start did not change the volume in the test above) and continue
from there. Anything else, or a collection that loads with fewer points: stop
Qdrant and restore the disk snapshot, then start again from the version the
snapshot was taken on.

## Local development stack

`docker-compose.yml` pins the same image. Its volume is
`<project>_qdrant_data`, where the project is the checkout's directory name
unless `COMPOSE_PROJECT_NAME` is set (`docker volume ls | grep qdrant_data`
lists them). A volume that holds collections made before v0.87.0 crash-loops
the new image after `make up`. Either step it through — the dev stack publishes Qdrant on
loopback without an API key, so no helper is needed:

```bash
cat > /tmp/qdrant-step.yml <<'EOF'
services:
  qdrant:
    image: qdrant/qdrant:${QDRANT_STEP_TAG:?set QDRANT_STEP_TAG}
EOF
for tag in v1.16.3 v1.17.1 v1.18.3; do
  QDRANT_STEP_TAG=$tag docker compose -f docker-compose.yml -f /tmp/qdrant-step.yml up -d --no-deps qdrant
  timeout 300 sh -c 'until curl -fs http://127.0.0.1:6333/readyz >/dev/null; do sleep 2; done' || break
done
docker compose up -d --no-deps qdrant   # v1.19.1
```

or, for disposable data, drop the volume and let the stack create a fresh one
(memories already in PostgreSQL then have no vectors, so recall does not find
them):

```bash
docker compose rm -sf qdrant && docker volume rm <project>_qdrant_data && docker compose up -d qdrant
```

Kagura Lite deployments (`KAGURA_VECTOR_BACKEND=lance`, no Qdrant) are not affected.
