# PostgreSQL 15 → 18.4 Migration Runbook (#1302)

Operator runbook for migrating the single-server production stack from
`postgres:15-alpine` to the digest-pinned `postgres:18.4-alpine`. This is a
**stateful cutover with downtime**, not a rolling deploy — schedule a
maintenance window.

> **Status**: executed against production on 2026-07-16 — evidence recorded on
> #1311 (rollback-window close-out tracked in its follow-up issue). The
> guard-window warnings below are retained as a record for future
> major-version migrations.

The pinned image lives in `docker-compose.prod.yml` (multi-arch
manifest-list digest, resolved 2026-07-16 from Docker Hub). This runbook
never hardcodes it — the Conventions section derives it from the compose
file so the two cannot drift.

## Why this is not an image-tag bump

The PostgreSQL 18+ Docker Official Image changed its storage layout:

| | PG ≤ 17 image | PG 18+ image |
|---|---|---|
| Default `PGDATA` | `/var/lib/postgresql/data` | `/var/lib/postgresql/18/docker` |
| Volume mount target | `/var/lib/postgresql/data` | `/var/lib/postgresql` |

**Never start the PG18 image against the existing PG15 volume
(`kagura_postgres_data`).** The image's own entrypoint refuses to start when
it detects an old-version data directory (`docker_error_old_databases`
guard), but it will happily `initdb` a **fresh empty** volume — which is the
real hazard here (see the deploy-guard note below). The compose files now
mount a new volume (`kagura_postgres_data_18`) at `/var/lib/postgresql`;
data moves via dump/restore (Option A). The old volume stays untouched — and
is deliberately no longer declared in `docker-compose.prod.yml`, so
`docker compose down -v` cannot delete it during the rollback window.

## ⚠ Guard against accidental early cutover

Between merging #1302 and executing this runbook, the repo's compose
definition (PG18, empty volume) diverges from the running production
container (PG15, real data). During that window:

- `scripts/deploy.sh` is safe: its api-* paths use `--no-deps` (added in
  #1302, matching the pre-existing web path) so a routine deploy never
  recreates postgres.
- A raw `dc up -d` (or `systemctl restart kagura-memory`) is **NOT** safe:
  it recreates postgres onto the empty PG18 volume, `alembic upgrade head`
  then builds a fresh schema, readiness passes, and production silently
  serves an empty database. Keep the merge-to-cutover window short and do
  not run whole-stack `up -d` in it.
- CI runs against PG18.4 from the moment #1302 merges, while prod is still
  PG15 until cutover. Do not merge Alembic migrations that rely on
  PG16+/PG18-only behavior inside this window.

## Method decision

- **Option A — dump/restore (default, this runbook).** Right-sized for a
  single-host database; rebuilds all indexes, which also makes the
  15-alpine → 18-alpine (musl/ICU) transition collation-safe.
- **Option B — `pg_upgrade`.** Only if measured dump/restore downtime exceeds
  the agreed window. Additional hard requirements if chosen: same-libc images
  (alpine → alpine only), `pg_upgrade --check` in rehearsal, both versioned
  data dirs under one `/var/lib/postgresql` mount, and post-upgrade
  `amcheck`/`REINDEX` validation of text indexes — carrying data files across
  a collation-semantics change silently corrupts text indexes. Also: PG18's
  `initdb` enables **data checksums by default** while the PG15 cluster has
  them off — `pg_upgrade` refuses mismatched clusters, so initdb the new
  cluster with `POSTGRES_INITDB_ARGS=--no-data-checksums` (or run
  `pg_checksums --enable` on the old cluster first).

Record the chosen method and rehearsal evidence on issue #1302 before the
production cutover.

## Conventions used below

Run on the production VM from the directory holding `docker-compose.prod.yml`
(`/opt/kagura-memory/src/terraform/single-server`; the blue/green marker is
`/opt/kagura-memory/active-color`). Set up the shell once:

```bash
cd /opt/kagura-memory/src/terraform/single-server

dc() { docker compose -f docker-compose.prod.yml --env-file .env.prod "$@"; }

# .env.prod is only read by compose — export it for the raw `docker run`
# backup/restore commands below (they need $DB_PASSWORD):
set -a; . ./.env.prod; set +a

# The pinned PG18 image, derived from compose so this doc can't drift:
PG18_IMAGE=$(dc config | awk '$1 == "image:" && $2 ~ /^postgres:/ {print $2; exit}')

# The compose network (expected: single-server_default — project name comes
# from the compose file's directory; see terraform/single-server/README.md):
NET=$(docker inspect kagura-postgres \
  -f '{{range $k,$_ := .NetworkSettings.Networks}}{{$k}}{{end}}')

echo "image=$PG18_IMAGE net=$NET"   # sanity-check both before proceeding
```

Backups are taken with the **PG18 client tools from the pinned image** —
never host binaries of unknown version (newer client dumping the older
server is the supported direction).

## 1. Preflight (record everything on issue #1302)

- [ ] Application commit/tag deployed, and Alembic head:
      `dc exec api-$(cat /opt/kagura-memory/active-color) alembic current`
- [ ] Live server version + **image digest** (rollback re-pulls by this
      digest, since the running PG15 container will not survive the cutover):
      `dc exec postgres psql -U kagura -d kagura -c 'select version()'` and
      `docker inspect --format '{{index .RepoDigests 0}}' $(docker inspect --format '{{.Image}}' kagura-postgres)`
- [ ] Database size: `dc exec postgres psql -U kagura -d kagura -c "select pg_size_pretty(pg_database_size('kagura'))"`
- [ ] Row counts for critical tables (memories, contexts, users, workspaces,
      memory_access_events) — keep the psql output verbatim.
- [ ] Encoding / collation / timezone (use `pg_database` — the `lc_collate`
      GUC was removed in PostgreSQL 16, so `show lc_collate` errors on 18):
      `dc exec postgres psql -U kagura -d kagura -c "show server_encoding; show timezone; select datcollate, datctype from pg_database where datname='kagura'"`
- [ ] Roles and grants: `dc exec postgres psql -U kagura -d postgres -c '\du'`
- [ ] **Database-level properties** — neither `--globals-only` nor a
      non-`--create` dump carries them, so record them for manual re-apply
      after restore: `dc exec postgres psql -U kagura -d postgres -c '\l+'`
      and `dc exec postgres psql -U kagura -d postgres -c 'select * from pg_db_role_setting'`
      (any `ALTER DATABASE kagura SET ...` GUC or database-level GRANT shown
      here must be re-applied in §4 step 7 and checked in step 8).
- [ ] Schema ownership: `dc exec postgres psql -U kagura -d kagura -c '\dn+'`.
      If `public` is NOT owned by `pg_database_owner`, the dump will contain
      `CREATE SCHEMA public` and collide with the initdb-created schema under
      `--exit-on-error` — plan to `DROP SCHEMA public` in the fresh `kagura`
      DB right before the restore in that case.
- [ ] Extensions (expected: `pg_trgm` + defaults):
      `dc exec postgres psql -U kagura -d kagura -c '\dx'`
- [ ] Free disk ≥ (old volume + dump + new volume) — `df -h`,
      `docker system df -v`.
- [ ] **Pre-stage the window**: `docker pull "$PG18_IMAGE"` and `git fetch`
      now, so no registry/network dependency remains inside the window.
- [ ] **Disable the weekly prune cron for the window** (re-enabled in §6):
      `sudo chmod -x /etc/cron.weekly/docker-prune`. The cron (installed by
      `startup.sh`) runs `docker volume prune -f --filter "label!=keep"` and
      `docker system prune -af --filter "until=168h"`; after cutover
      `kagura_postgres_data` becomes an unreferenced named volume, and
      whether volume prune removes it depends on engine version semantics
      (< 23 removes named dangling volumes; the volume also has no `keep`
      label). Do not gamble the sole rollback data source on that — disable
      the cron unconditionally through the rollback window. The system prune
      would also remove the unused PG15 *image* within a week — rollback
      therefore re-pulls by the digest recorded above, never by the floating
      tag.
- [ ] Agree the maintenance window and the rollback decision point (§5).

## 2. Backup procedure (used by rehearsal §3 and cutover §4)

```bash
BACKUP_DIR=/var/lib/kagura/pg18-migration/$(date -u +%Y%m%dT%H%M%SZ)
sudo mkdir -p "$BACKUP_DIR"

# Globals (roles/grants) — tiny, serial is fine:
docker run --rm --network "$NET" \
  -e PGPASSWORD="$DB_PASSWORD" -v "$BACKUP_DIR":/backup \
  "$PG18_IMAGE" \
  pg_dumpall -h postgres -U kagura --globals-only -f /backup/globals.sql

# Application database — directory format with parallel jobs (the dump runs
# inside the downtime window at cutover; parallelism cuts it by ~core count):
docker run --rm --network "$NET" \
  -e PGPASSWORD="$DB_PASSWORD" -v "$BACKUP_DIR":/backup \
  "$PG18_IMAGE" \
  pg_dump -h postgres -U kagura -Fd -j "$(nproc)" -d kagura -f /backup/kagura.dump.d

# /var/lib/kagura is root-owned 0700 and the dumps are written as root:
sudo sh -c "cd '$BACKUP_DIR' && sha256sum globals.sql \$(find kagura.dump.d -type f) > SHA256SUMS"
```

Copy `$BACKUP_DIR` off-VM (mandatory for the final cutover backup — the copy
may run in the background in parallel with §4 steps 4–9; only §6 cleanup is
gated on it being confirmed).

**Volume snapshot** — only meaningful with postgres **stopped** (a tar of a
live data directory is torn); at cutover it is taken at §4 step 4, after
`dc stop postgres`. Preferred method on this GCP VM is a disk snapshot —
see "Manual snapshot" in `terraform/single-server/README.md`
(`gcloud compute disks snapshot ${GCP_VM} ...`; crash-consistent, fast,
incremental). A `tar` of `kagura_postgres_data` from a helper container is
the fallback.

## 3. Rehearsal (against a copy — never the only production volume)

- [ ] Restore `globals.sql` + `kagura.dump.d` into a scratch PG18.4 container
      on a fresh volume (same commands as §4 step 7); verify checksums first.
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
- [ ] Execute a rollback rehearsal (§5) and record its duration.
- [ ] `docker scout cves "$PG18_IMAGE"` (or equivalent scanner) — record
      accepted/fixed findings.

## 4. Production cutover

Announce the window. Then:

1. **Suppress every auto-restart path for the window.** `restart: always`
   revives manually-stopped containers on any dockerd restart, and the
   `kagura-memory` systemd unit runs whole-stack `up -d` at boot (its
   `up -d` would also recreate postgres onto the empty volume mid-window):

   ```bash
   sudo systemctl disable kagura-memory
   docker update --restart=no kagura-api-blue kagura-api-green kagura-postgres
   ```

2. **Fence all writers.** Background schedulers (APScheduler) run inside the
   API containers, so stopping **both colors** stops them too:

   ```bash
   dc stop api-blue api-green
   ```

   Leave `caddy`/`web` up (they will 502 the API; static pages keep serving)
   or stop them too for a full-outage banner — operator's choice.

   **With writers fenced, re-run the §1 row-count queries now** — these
   post-fence numbers (not the days-old preflight ones) are the authoritative
   reference for step 8's validation.
3. **Final backup** per §2 (postgres is still running — the dump needs it).
   Verify checksums; start the off-VM copy in the background.
4. **Stop PG15 and snapshot its volume:** `dc stop postgres`, then take the
   volume snapshot per §2 (gcloud disk snapshot preferred). Only the
   **volume** `kagura_postgres_data` survives this cutover — the PG15
   *container* is removed at step 6 when compose recreates the service, and
   the PG15 *image* will be pruned by the weekly cron; rollback uses the
   volume + the digest recorded in §1.
5. **Deploy the PG18 compose definition:** `git checkout <the merged #1302
   commit/tag>` (fetched in preflight). Confirm `docker-compose.prod.yml`
   pins 18.4 and mounts `postgres_data_18:/var/lib/postgresql`.
6. **Start PG18 on the fresh volume:** `dc up -d postgres`. This `initdb`s
   `kagura_postgres_data_18` (UTF8, UTC, role+DB `kagura` from the
   `POSTGRES_*` env). Do **not** trust the compose healthcheck alone here:
   `pg_isready` probes the Unix socket, which the entrypoint's temporary
   bootstrap server (TCP off) also answers during first-boot init. Wait for
   `dc logs postgres` to show `PostgreSQL init process complete` followed by
   `database system is ready to accept connections`, then confirm over TCP.
   Re-resolve the network var for the new container first:

   ```bash
   NET=$(docker inspect kagura-postgres -f '{{range $k,$_ := .NetworkSettings.Networks}}{{$k}}{{end}}')
   docker run --rm --network "$NET" "$PG18_IMAGE" pg_isready -h postgres -U kagura -d kagura
   ```

   **Re-entry note:** if any later step fails and you must retry from here,
   reset the half-written target first — `dc stop postgres && docker volume
   rm kagura_postgres_data_18` — so `dc up -d postgres` re-initdbs cleanly
   (the restore is `--exit-on-error` and not idempotent; a second run against
   a half-restored DB aborts on the first "already exists").
7. **Restore.** Globals first — plain `psql -f` continues past errors and
   exits 0, so capture the output and require that the only errors are the
   expected "already exists" for the initdb-created role/attributes:

   ```bash
   docker run --rm --network "$NET" \
     -e PGPASSWORD="$DB_PASSWORD" -v "$BACKUP_DIR":/backup \
     "$PG18_IMAGE" psql -h postgres -U kagura -d postgres -f /backup/globals.sql \
     2>&1 | tee /tmp/globals-restore.log
   grep '^ERROR' /tmp/globals-restore.log | grep -v 'already exists' && \
     echo "UNEXPECTED globals errors — stop and investigate" || echo "globals OK"

   docker run --rm --network "$NET" \
     -e PGPASSWORD="$DB_PASSWORD" -v "$BACKUP_DIR":/backup \
     "$PG18_IMAGE" pg_restore -h postgres -U kagura -d kagura \
       --no-owner --role=kagura --exit-on-error -j "$(nproc)" /backup/kagura.dump.d
   ```

   Then re-apply any database-level settings/GRANTs recorded in §1's `\l+` /
   `pg_db_role_setting` output — dumps do not carry them.

8. **Validate before reopening writes.** Both API colors are stopped, so run
   alembic in a one-off container (`dc exec` fails against stopped services;
   do NOT `dc up` an api color just to exec into it — that reopens traffic
   through Caddy before validation):

   ```bash
   dc run --rm --no-deps api-blue alembic current
   dc run --rm --no-deps api-blue alembic upgrade head   # must be a no-op
   ```

   Compare against the records: extensions (`pg_trgm` present), schema diff
   clean, row counts match the **post-fence** numbers from step 2, sequences
   advanced to recorded values, `datcollate`/`datctype`/encoding match §1,
   database-level settings re-applied (step 7), `alembic current` == recorded
   head.
9. **Reopen:**

   ```bash
   dc up -d --no-deps api-$(cat /opt/kagura-memory/active-color)
   # wait for /readiness, then restore the auto-restart paths:
   docker update --restart=always kagura-api-blue kagura-api-green kagura-postgres
   sudo systemctl enable kagura-memory
   ```

   Verify `/health`, REST, MCP, login/auth, `remember`/`recall`, background
   tasks (scheduler logs), admin CLI.
10. **Monitor through the rollback window:** postgres logs
    (`dc logs -f postgres`), connection errors, latency, locks
    (`pg_stat_activity`), disk usage.

## 5. Rollback

**Decision point: the moment writes are accepted on PG18 (§4 step 9).**

- **Before writes are accepted:** safe and fast. `dc stop postgres`,
  `git checkout` the pre-#1302 compose (PG15 + `kagura_postgres_data`),
  pin the postgres image to the **digest recorded in §1** (edit the compose
  `image:` to `postgres@sha256:<recorded>` — the floating `15-alpine` tag
  may have moved, and the local PG15 image may already be pruned),
  `dc up -d postgres`, validate, `dc up -d --no-deps api-<active>`. The PG15
  volume was never touched. PG18 data files can **not** be opened by PG15 —
  rollback is always "switch back to the old volume", never "downgrade in
  place".
- **After writes are accepted:** forward-fix is the default. Rolling back now
  loses post-cutover writes unless you take a reverse logical export
  (PG18 `pg_dump` → restore into PG15) — decide and document per incident.
  Any restore of an older backup must **re-apply completed erasure
  requests** (`docs/ops/erasure-runbook.md` — GDPR obligation).
- Do **not** delete `kagura_postgres_data` or the PG15 backups until the
  rollback window is formally closed on #1302. (The PG15 container/image are
  already gone — see §4 step 4 — that is expected and not a data-loss
  signal.)

## 6. Cleanup (after the window is formally closed on #1302)

- [ ] Confirm the off-VM copy of the final backup (checksums re-verified).
- [ ] `docker volume rm kagura_postgres_data`
- [ ] Remove `postgres:15-alpine` if still present: `docker rmi
      postgres:15-alpine` (plain `docker image prune` skips tagged images;
      `prune -a` would also sweep the previous api/web images deploy.sh
      rollback depends on).
- [ ] Re-enable the weekly prune cron disabled in §1:
      `sudo chmod +x /etc/cron.weekly/docker-prune`
- [ ] Retire old dumps per the backup-retention policy — **max backup age
      90 days** and erasure re-apply rules per `docs/ops/erasure-runbook.md`
      §3; the final PG15 backup is subject to the same 90-day ceiling.
- [ ] Close #1302 with links to the recorded evidence.

## Local development note

`docker-compose.yml` uses the same pinned PG18.4 image with a fresh
`postgres_data_18` volume. Local PG15 data is not migrated automatically —
rebuild the schema with `docker exec kagura-api alembic upgrade head`
(`make migrate` requires a host venv), or restore a dump the same way as §4
step 7. The old PG15 volume (project-prefixed, e.g.
`memory-cloud_postgres_data` — confirm with `docker volume ls | grep
postgres_data`) can be removed whenever you no longer need it.
