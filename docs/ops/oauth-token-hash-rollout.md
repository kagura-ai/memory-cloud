# OAuth token hash storage rollout

Issue #2399 changes the database representation of OAuth / MCP access and
refresh tokens. It does not change the tokens returned to clients, token
lifetimes, scopes, public-client behavior, rotation or revocation semantics.
OAuth client secrets are a separate credential: confidential clients already
store their secret as a hash, and public clients have no client secret.

This is an operator procedure, not evidence that any deployment has completed
it. A merged change or successful Alembic upgrade alone does not establish
hash-only storage. Completion requires the finalization and verification below.

## Storage states and compatibility

The additive `f20_2399_oauth_token_storage` migration creates the singleton
`oauth_token_storage` row
(`id = 1`) in `bridge` mode, even on an empty database. It does not rewrite
`oauth_tokens` or enable the hash-only constraint automatically.

| State | Readers | Issuance | Existing token rows |
|---|---|---|---|
| Before compatible deployment | Old binaries read raw tokens only | Raw tokens | Raw tokens |
| `bridge` | New binaries accept raw or hashed storage; old binaries still require raw storage | New binaries read the durable mode on every issuance and continue raw writes | Unchanged by the migration |
| `hashed` | Hash-compatible binaries required | Hashes only | Every access token and non-null refresh token converted; database constraint enforced |

A stored value has the form `sha256$` followed by 64 lowercase hex digits.
The prefix identifies a stored digest; it is never a valid bearer credential.
The original high-entropy token is still required for access, refresh,
revocation and introspection. The existing unique indexes remain in use.

The deployment script runs Alembic **before** starting the new API color and
before draining the old color (`terraform/single-server/scripts/deploy.sh`).
Consequently, schema upgrade must remain compatible with old readers and
writers. Do not add the finalization command to the pre-start migration step.

## 1. Deploy the compatible release

Use the normal deployment procedure to apply the additive schema migration and
roll out the hash-compatible binary to every API instance. Include background
workers, scheduled jobs and any separate process that reads or issues OAuth
tokens. The database stays in `bridge` mode throughout this step.

Using an authorized PostgreSQL session, check the durable state without
selecting any token values:

```sql
SELECT mode FROM oauth_token_storage WHERE id = 1;
```

For a first rollout, expect exactly one row containing `bridge`. If it already
contains `hashed`, keep all running binaries hash-compatible; do not revert it
to `bridge`. A missing row or unexpected value is an error to investigate before
continuing.

## 2. Drain every old reader and writer

Check the actual running image versions and deployment inventory, rather than
only the load balancer's active color. Remove old instances from traffic, wait
for their in-flight requests and database transactions to finish, and stop them.
Stop or replace old workers, scheduled jobs, one-off commands and any other
process that could still read or write `oauth_tokens`. Disable automatic
restarts of an old image.

The `--writers-drained` flag is the operator's acknowledgement of this check;
the CLI cannot discover all deployed processes. **Old readers must also be
drained**: they cannot authenticate a token after its stored value is hashed.
An empty token table is not evidence that old processes are gone.

For a fresh installation with no old processes, this condition is already met.
Finalize immediately after the schema migration, preferably before opening
traffic or issuing the first token. Fresh installations otherwise also remain
in `bridge` mode.

## 3. Finalize storage

Run once from the compatible backend environment connected to the intended
database. The example is for a checkout with its backend virtual environment
and normal database settings already configured:

```bash
cd backend
PYTHONPATH=src uv run python -m auth.oauth_token_storage \
  --writers-drained --lock-timeout-ms 5000
```

For the standard blue/green deployment, run in the verified active, compatible
API container. Replace `kagura-api-green` with the actual container name:

```bash
API_CONTAINER=kagura-api-green
docker exec --workdir /app --env PYTHONPATH=/app/src "$API_CONTAINER" \
  python -m auth.oauth_token_storage --writers-drained --lock-timeout-ms 5000
```

Do not put database credentials or token values into command-line arguments,
terminal output or issue comments. The command uses the backend's configured
database connection.

Finalization runs in one fresh database transaction. It takes an
`ACCESS EXCLUSIVE` lock on `oauth_tokens`, converts all raw access and non-null
refresh tokens in place (including revoked and expired rows), adds and validates
`ck_oauth_tokens_hashed`, verifies that no non-hash values remain, and sets the
singleton mode to `hashed`. The conversion, constraint and state change commit
together. Already hashed values are not hashed a second time.

The exclusive lock also covers compatible issuance: issuers acquire their table
lock before reading the uncached mode, so a request cannot decide to write raw
storage before finalization and commit that raw value afterward. The database
constraint rejects any later raw insert or update.

Choose a quiet period. `--lock-timeout-ms` bounds the wait for a database lock;
it does not bound the time spent converting and validating the table. Token
reads and writes can wait while the exclusive lock is held. Large tables require
an appropriately planned maintenance window.

## 4. Verify completion

In an authorized PostgreSQL session, run these checks. They return only state,
counts and constraint metadata:

```sql
SELECT mode FROM oauth_token_storage WHERE id = 1;

SELECT count(*) AS non_hash_token_rows
FROM oauth_tokens
WHERE access_token !~ '^sha256\$[0-9a-f]{64}$'
   OR (refresh_token IS NOT NULL
       AND refresh_token !~ '^sha256\$[0-9a-f]{64}$');

SELECT conname, convalidated
FROM pg_constraint
WHERE conrelid = 'oauth_tokens'::regclass
  AND conname = 'ck_oauth_tokens_hashed';
```

Require `hashed`, `non_hash_token_rows = 0`, and one validated constraint
(`convalidated = true`). There is deliberately no filter for expiry or
revocation: obsolete token rows must also contain hashes.

Exercise a controlled client's existing access token, refresh rotation, newly
issued access token, introspection with the appropriate client authentication,
and revocation. Confirm that the previously issued raw token still works where
its lifetime and scope permit, and that revoked credentials fail normally.
The [Remote OAuth verification runbook](remote-oauth-verification.md) covers
the end-to-end protocol checks without recording secrets. Recheck the database
counts after issuing and refreshing a token to confirm new writes stay hashed.

Record the release/image, database migration revision, finalization time, state,
zero count and protocol-check results. Never record raw tokens or their hashes.
Only then describe this deployment's live OAuth token storage as hash-only.

## Failure, retry and rollback

- If a lock wait times out, finalization exits with an error. Investigate the
  blocking session and choose another window; do not terminate unrelated
  transactions blindly. The transaction rolls back its conversion, constraint
  and mode update together, leaving the prior committed state intact.
- Any error before commit likewise rolls back the whole finalization. There is
  no partially committed backfill to repair manually. If the connection is lost
  around commit, use the verification queries to determine the durable result.
- Rerunning finalization is safe. It preserves existing hashes and verifies the
  hash-only invariant; it does not require clients to reconnect or rotate tokens.
- Before finalization, an ordinary bridge-only rollout can be rolled back to
  old binaries while storage is still raw. Verify the actual database state
  first. Do not assume the presence of the additive schema proves raw storage.
- After finalization, **do not roll back to a binary that only understands raw
  token storage**. Keep a hash-compatible image available. The migration's
  downgrade refuses to undo finalized storage; hashes cannot reconstruct the
  original tokens. Changing `mode`, dropping the constraint or running an old
  image does not reverse the data conversion.

Hashing the live table does not rewrite historical database backups, WAL or
snapshots. Keep their existing access restrictions and retention controls; do
not claim that finalization has removed token material from historical copies.
