# PostgreSQL 15 → 18.4 Migration Runbook (#1302)

Operator runbook for migrating the single-server production stack from
`postgres:15-alpine` to the digest-pinned `postgres:18.4-alpine`. This is a
**stateful cutover with downtime**, not a rolling deploy — schedule a
maintenance window.

Pinned image (multi-arch manifest-list digest, resolved 2026-07-16 from
Docker Hub; last pushed 2026-07-08):

```
postgres:18.4-alpine@sha256:9a8afca54e7861fd90fab5fdf4c42477a6b1cb7d293595148e674e0a3181de15
```

## Why this is not an image-tag bump

The PostgreSQL 18+ Docker Official Image changed its storage layout:

| | PG ≤ 17 image | PG 18+ image |
|---|---|---|
| Default `PGDATA` | `/var/lib/postgresql/data` | `/var/lib/postgresql/18/docker` |
| Volume mount target | `/var/lib/postgresql/data` | `/var/lib/postgresql` |

**Never start the PG18 image against the existing PG15 volume
(`kagura_postgres_data`).** The compose files now mount a fresh volume
(`kagura_postgres_data_18`) at `/var/lib/postgresql`; data moves via
dump/restore (Option A below). The old volume stays untouched — and is
deliberately no longer declared in `docker-compose.prod.yml`, so
`docker compose down -v` cannot delete it during the rollback window.

## Method decision

- **Option A — dump/restore (default, this runbook).** Right-sized for a
  single-host database; rebuilds all indexes, which also makes the
  15-alpine → 18-alpine (musl/ICU) transition collation-safe.
- **Option B — `pg_upgrade`.** Only if measured dump/restore downtime exceeds
  the agreed window. Additional hard requirements if chosen: same-libc images
  (alpine → alpine only), `pg_upgrade --check` in rehearsal, both versioned
  data dirs under one `/var/lib/postgresql` mount, and post-upgrade
  `amcheck`/`REINDEX` validation of text indexes — carrying data files across
  a collation-semantics change silently corrupts text indexes.

Record the chosen method and rehearsal evidence on issue #1302 before the
production cutover.

## Conventions used below

- Run on the production VM from the directory holding
  `docker-compose.prod.yml` (`terraform/single-server/` in the checked-out
  repo; the blue/green marker lives at `/opt/kagura-memory/active-color`).
- `dc` is the same alias `scripts/deploy.sh` uses:

  ```bash
  dc() { docker compose -f docker-compose.prod.yml --env-file .env.prod "$@"; }
  ```

- `PG18_IMAGE` refers to the pinned image string at the top of this runbook.
- Backups are taken with the **PG18 client tools from the pinned image** —
  never host binaries of unknown version (newer client dumping the older
  server is the supported direction).

## 1. Preflight (record everything on issue #1302)

- [ ] Application commit/tag deployed, and Alembic head:
      `dc exec api-$(cat /opt/kagura-memory/active-color) alembic current`
- [ ] Live server version + image digest:
      `dc exec postgres psql -U kagura -d kagura -c 'select version()'` and
      `docker inspect --format '{{index .RepoDigests 0}}' $(docker inspect --format '{{.Image}}' kagura-postgres)`
- [ ] Database size: `dc exec postgres psql -U kagura -d kagura -c "select pg_size_pretty(pg_database_size('kagura'))"`
- [ ] Row counts for critical tables (memories, contexts, users, workspaces,
      memory_access_events) — keep the psql output verbatim.
- [ ] Encoding / locale / timezone:
      `dc exec postgres psql -U kagura -d kagura -c 'show server_encoding; show lc_collate; show timezone'`
- [ ] Roles and grants: `dc exec postgres psql -U kagura -d postgres -c '\du'`
- [ ] Extensions (expected: `pg_trgm` + defaults):
      `dc exec postgres psql -U kagura -d kagura -c '\dx'`
- [ ] Free disk ≥ (old volume + dump + new volume) — check `df -h` and
      `docker system df -v`.
- [ ] Agree the maintenance window and the rollback decision point (§6).

## 2. Backup (rehearsal AND final cutover use the same procedure)

```bash
BACKUP_DIR=/var/lib/kagura/pg18-migration/$(date -u +%Y%m%dT%H%M%SZ)
sudo mkdir -p "$BACKUP_DIR"

# Globals (roles/grants) — PG18 client over the compose network:
docker run --rm --network kagura-memory_default \
  -e PGPASSWORD="$DB_PASSWORD" -v "$BACKUP_DIR":/backup \
  "$PG18_IMAGE" \
  pg_dumpall -h postgres -U kagura --globals-only -f /backup/globals.sql

# Application database, custom format:
docker run --rm --network kagura-memory_default \
  -e PGPASSWORD="$DB_PASSWORD" -v "$BACKUP_DIR":/backup \
  "$PG18_IMAGE" \
  pg_dump -h postgres -U kagura -Fc -d kagura -f /backup/kagura.dump

sha256sum "$BACKUP_DIR"/* | sudo tee "$BACKUP_DIR/SHA256SUMS"
```

> The compose network name is `<project>_default`; confirm with
> `docker network ls`. Off-VM copy of `$BACKUP_DIR` is mandatory for the
> final cutover backup.

Additionally take a volume-level snapshot of `kagura_postgres_data`
(stopped-container `tar` of the volume, or the provider's disk snapshot).

## 3. Rehearsal (against a copy — never the only production volume)

- [ ] Restore `globals.sql` + `kagura.dump` into a scratch PG18.4 container on
      a fresh volume; verify checksums first.
- [ ] Compare schema (`pg_dump -s` diff), roles, extensions, table counts,
      critical row counts, sequence values (`select * from pg_sequences`),
      and a handful of representative application queries against §1 records.
- [ ] `alembic current` on the restored DB must equal the recorded head, and
      `alembic upgrade head` must be a no-op (no drift).
- [ ] CI already runs the integration/migration suites against PG18.4
      (`.github/workflows/ci.yml`); additionally run `make test-integration`
      pointed at the rehearsal DB if rehearsing off-VM.
- [ ] Run API/MCP smoke: `/health`, `/readiness`, login, `remember`/`recall`,
      admin CLI, background-job startup logs.
- [ ] Measure: dump duration, restore duration → expected production
      downtime. Record on #1302.
- [ ] Execute a rollback rehearsal (§6) and record its duration.
- [ ] `docker scout cves "$PG18_IMAGE"` (or equivalent scanner) — record
      accepted/fixed findings.

## 4. Production cutover

Announce the window. Then:

1. **Fence all writers.** Background schedulers (APScheduler) run inside the
   API containers, so stopping both colors stops them too. Stop **both**
   colors — the idle one must not be able to come back mid-migration:

   ```bash
   dc stop api-blue api-green
   ```

   Leave `caddy`/`web` up (they will 502 the API; static pages keep serving)
   or stop them too for a full-outage banner — operator's choice.
2. **Final backup** per §2, plus the volume snapshot. Verify checksums and
   copy off-VM before proceeding.
3. **Stop PG15:** `dc stop postgres` (container stays for rollback; do NOT
   `rm` it or its volume).
4. **Deploy the PG18 compose definition:** `git fetch && git checkout <the
   merged #1302 commit/tag>` in the repo working copy on the VM. Confirm
   `docker-compose.prod.yml` now pins 18.4 and mounts
   `postgres_data_18:/var/lib/postgresql`.
5. **Start PG18 on the fresh volume:** `dc up -d postgres` → wait healthy
   (`dc ps postgres`). This `initdb`s `kagura_postgres_data_18` with the
   `POSTGRES_*`/`TZ`/`PGTZ` env from compose.
6. **Restore:**

   ```bash
   docker run --rm --network kagura-memory_default \
     -e PGPASSWORD="$DB_PASSWORD" -v "$BACKUP_DIR":/backup \
     "$PG18_IMAGE" psql -h postgres -U kagura -d postgres -f /backup/globals.sql
   docker run --rm --network kagura-memory_default \
     -e PGPASSWORD="$DB_PASSWORD" -v "$BACKUP_DIR":/backup \
     "$PG18_IMAGE" pg_restore -h postgres -U kagura -d kagura \
       --no-owner --role=kagura --exit-on-error /backup/kagura.dump
   ```

   (`globals.sql` will error on pre-existing role `kagura` created by
   `initdb` env — `CREATE ROLE` failing with "already exists" is expected;
   everything else must apply cleanly.)
7. **Validate before reopening writes** — §1 comparisons: extensions
   (`pg_trgm` present), schema diff clean, row counts match the final backup,
   sequences advanced to recorded values, `alembic current` == recorded head,
   `alembic upgrade head` no-op.
8. **Reopen:** `dc up -d api-$(cat /opt/kagura-memory/active-color)` → wait
   `/readiness`; then verify `/health`, REST, MCP, login/auth,
   `remember`/`recall`, background tasks (scheduler logs), admin CLI.
9. **Monitor through the rollback window:** postgres logs
   (`dc logs -f postgres`), connection errors, latency, locks
   (`pg_stat_activity`), disk usage.

## 5. Rollback

**Decision point: the moment writes are accepted on PG18 (§4 step 8).**

- **Before writes are accepted:** safe and fast. `dc stop postgres`,
  `git checkout` the pre-#1302 compose (PG15 + `kagura_postgres_data`),
  `dc up -d postgres`, validate, `dc up -d api-<active>`. The PG15 volume was
  never touched. PG18 data files can **not** be opened by PG15 — rollback is
  always "switch back to the old volume", never "downgrade in place".
- **After writes are accepted:** forward-fix is the default. Rolling back now
  loses post-cutover writes unless you take a reverse logical export
  (PG18 `pg_dump` → restore into PG15) — decide and document per incident.
- Do **not** delete `kagura_postgres_data`, the PG15 backups, or the stopped
  PG15 image/container until the rollback window is formally closed on #1302.

## 6. Cleanup (after the window is formally closed on #1302)

- [ ] `docker volume rm kagura_postgres_data`
- [ ] Remove retired dumps per backup-retention policy (keep the final PG15
      backup per policy, off-VM).
- [ ] `docker image prune` the PG15 image.
- [ ] Close #1302 with links to the recorded evidence.

## Local development note

`docker-compose.yml` now uses the same pinned PG18.4 image with a fresh
`postgres_data_18` volume. Local PG15 data is not migrated automatically —
run `make migrate` for a fresh schema (or restore a local dump the same way
as §4.6). The old `memory-cloud_postgres_data` volume can be removed whenever
you no longer need it.
