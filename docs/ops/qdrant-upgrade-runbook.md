# Qdrant 1.15 → 1.19 Upgrade Runbook (#1793)

From v0.87.0 the compose files pin `qdrant/qdrant:v1.19.1` and the backend
ships `qdrant-client` 1.18. A Qdrant volume created by an earlier release
(server 1.15) **cannot start on 1.19.1 directly**: it has to be upgraded one
minor version at a time. This runbook is that procedure for the single-server
stack (single-host and split-host layouts) and for a local development stack.

A routine `./scripts/deploy.sh` never recreates Qdrant (`--no-deps`), so the
deploy itself does not touch the volume. What does is any command that
recreates the `qdrant` service from the new compose files:

- the `kagura-memory` systemd unit, which runs a whole-stack `up -d` at boot —
  once the checkout is on v0.87.0, the next reboot is enough;
- the data-tier recreate in [Container log rotation](../deployment.md#container-log-rotation);
- the data VM's `docker compose … up -d` on a split host;
- `make up` on a dev machine.

So deploy v0.87.0 as step 3 of this runbook, inside one maintenance window,
rather than on its own.

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

Run everything on the VM from `/opt/kagura-memory/src/terraform/single-server`,
in **one shell session** — the helpers below live in it.

- **Maintenance window.** Each step restarts Qdrant. While it loads, recall and
  any write that needs the vector store fail; the restart takes seconds on a
  small collection and grows with its size.
- **Disable the boot unit for the window** — re-enable it after step 5:

  ```bash
  sudo systemctl disable kagura-memory
  ```

- **Back up the Qdrant volume.** Check `df -h /var/lib/docker` first; the archive
  is about the size of the collections.

  ```bash
  docker stop kagura-qdrant
  sudo tar -C "$(docker volume inspect -f '{{.Mountpoint}}' kagura_qdrant_data)" \
    -czf "/var/backups/qdrant-$(date +%Y%m%d-%H%M).tgz" .
  docker start kagura-qdrant
  ```

  A [disk snapshot](../../terraform/single-server/README.md#manual-snapshot)
  works too, but restoring one also rewinds PostgreSQL and Redis.

## Procedure — single host

Helpers: an overlay that swaps only the image (so every step runs from the same
service definition — network, volume, API key — as the real one);
`qdrant_status`, which reads the server through a running API container (the
Qdrant image has no curl; the API container has the URL and the key in its
environment) and prints its version and each collection's status and point
count; `wait_qdrant`, which waits until a given version serves; and
`qdrant_step`, which runs one overlay step and waits for it.

```bash
W="$(mktemp -d)"
cat > "$W/qdrant-step.yml" <<'EOF'
services:
  qdrant:
    image: qdrant/qdrant:${QDRANT_STEP_TAG:?set QDRANT_STEP_TAG}
EOF
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
wait_qdrant() {  # $1 = the version that must be serving, e.g. 1.16.3; up to 5 minutes
  local out
  for _ in $(seq 1 60); do
    if out="$(qdrant_status 2>/dev/null)" && [ "$(head -n1 <<<"$out")" = "version $1" ]; then
      printf '%s\n' "$out"; return 0
    fi
    sleep 5
  done
  echo "Qdrant $1 is not serving: see 'If a step does not come up'" >&2; return 1
}
qdrant_step() {  # $1 = version, e.g. 1.16.3
  QDRANT_STEP_TAG="v$1" docker compose -f docker-compose.prod.yml -f "$W/qdrant-step.yml" \
    --env-file .env.prod up -d --no-deps qdrant && wait_qdrant "$1"
}

qdrant_status | tee "$W/before.txt"
```

Steps 1–2, with your current API still serving. `&&` stops at the first step
that does not come up — never go on from a failed step:

```bash
qdrant_step 1.16.3 && qdrant_step 1.17.1
```

Step 3 — deploy v0.87.0 as usual ([Update to a new release](../../terraform/single-server/README.md#update-to-a-new-release-zero-downtime)):

```bash
cd /opt/kagura-memory/src && git fetch && git reset --hard origin/main
cd terraform/single-server && ./scripts/deploy.sh
```

Steps 4–5. The last one uses the compose file alone, which leaves the service
exactly as the release defines it:

```bash
qdrant_step 1.18.3 \
  && docker compose -f docker-compose.prod.yml --env-file .env.prod up -d --no-deps qdrant \
  && wait_qdrant 1.19.1 > "$W/after.txt" && cat "$W/after.txt" \
  && diff <(tail -n +2 "$W/before.txt") <(tail -n +2 "$W/after.txt") && echo "collections unchanged"
```

Every collection should read `green` with the point count it had before. A
count that moved because the API took writes during the window is expected;
one that dropped to zero is not — see the next section. When it all reads
right, re-enable the boot unit:

```bash
sudo systemctl enable kagura-memory
```

## If a step does not come up

`docker logs --tail 50 kagura-qdrant` (on the data VM for a split host) names
the failure.

- **A `rocks_db` panic** means a minor was skipped. Run the step after the last
  version that served, then continue in order — in the test above the failed
  start did not change the volume.
- **A failed image pull** leaves the previous version running; `wait_qdrant`
  reports that it is not the expected version. Fix the pull and rerun the step.
- **Anything else, or a collection with fewer points:** restore the backup and
  start again from the version it was taken on:

  ```bash
  docker stop kagura-qdrant
  D="$(docker volume inspect -f '{{.Mountpoint}}' kagura_qdrant_data)"
  sudo find "$D" -mindepth 1 -delete
  sudo tar -C "$D" -xzf /var/backups/qdrant-<timestamp>.tgz
  qdrant_step 1.15.0     # the version the backup was taken on
  ```

## Procedure — split host

The Qdrant container runs on the **data VM**; the helpers need an API container
and run on the **app VM**. Disable whatever runs a whole-stack `up -d` at boot
on both VMs for the window.

On the data VM, in the directory with `docker-compose.data.yml` and its
`.env.prod` (`DATA_BIND_ADDR` as in README "Optional: split-host layout"):

```bash
W="$(mktemp -d)"
cat > "$W/qdrant-step.yml" <<'EOF'
services:
  qdrant:
    image: qdrant/qdrant:${QDRANT_STEP_TAG:?set QDRANT_STEP_TAG}
EOF
data_step() {  # $1 = version, e.g. 1.16.3
  DATA_BIND_ADDR=192.168.10.20 QDRANT_STEP_TAG="v$1" docker compose \
    -f docker-compose.data.yml -f docker-compose.data-expose.yml -f "$W/qdrant-step.yml" \
    --env-file .env.prod up -d --no-deps qdrant
}
```

Then, one step at a time, confirming each on the app VM (with `qdrant_status`
and `wait_qdrant` defined there as above) before the next:

| Step | Data VM | App VM |
|---|---|---|
| 1 | `data_step 1.16.3` | `wait_qdrant 1.16.3` |
| 2 | `data_step 1.17.1` | `wait_qdrant 1.17.1` |
| 3 | — | deploy v0.87.0 as README "Optional: split-host layout" describes |
| 4 | `data_step 1.18.3` | `wait_qdrant 1.18.3` |
| 5 | update the data VM's compose files to the release (e.g. `git fetch && git reset --hard origin/main` in its checkout), then `DATA_BIND_ADDR=192.168.10.20 docker compose -f docker-compose.data.yml -f docker-compose.data-expose.yml --env-file .env.prod up -d --no-deps qdrant` | `wait_qdrant 1.19.1` |

Step 5 needs the updated files: with the old ones, that command would start
Qdrant 1.15.0 on a volume that 1.18 has already written.

## Local development stack

`docker-compose.yml` pins the same image. Its volume is
`<project>_qdrant_data`, where the project is the checkout's directory name
unless `COMPOSE_PROJECT_NAME` is set (`docker volume ls | grep qdrant_data`
lists them). On a volume that holds collections made before v0.87.0, the new
image fails to start after `make up` (the dev service has no restart policy, so
the container just exits). Either step it through — the dev stack publishes
Qdrant on loopback without an API key, so no helper is needed:

```bash
W="$(mktemp -d)"
cat > "$W/qdrant-step.yml" <<'EOF'
services:
  qdrant:
    image: qdrant/qdrant:${QDRANT_STEP_TAG:?set QDRANT_STEP_TAG}
EOF
ok=1
for tag in 1.16.3 1.17.1 1.18.3; do
  QDRANT_STEP_TAG="v$tag" docker compose -f docker-compose.yml -f "$W/qdrant-step.yml" up -d --no-deps qdrant \
    && timeout 300 sh -c 'until curl -fs http://127.0.0.1:6333/readyz >/dev/null; do sleep 2; done' \
    && curl -fs http://127.0.0.1:6333/ | grep -q "\"version\":\"$tag\"" \
    || { ok=0; echo "stopped at $tag"; break; }
done
[ "$ok" = 1 ] && docker compose up -d --no-deps qdrant   # v1.19.1
```

or, for disposable data, drop the volume and let the stack create a fresh one
(memories already in PostgreSQL then have no vectors, so recall does not find
them):

```bash
docker compose rm -sf qdrant && docker volume rm <project>_qdrant_data && docker compose up -d qdrant
```

Kagura Lite deployments (`KAGURA_VECTOR_BACKEND=lance`, no Qdrant) are not affected.
