# Production checklist — Orel Server

## Architecture: Orel Server + CDB Local Server

```
SmartOps (ERP)  <-->  Orel Server  <-->  CDB Local Server  <-->  Azure Tenant
                       (this repo,          (../cdb-local-server,
                        PII masking/         relay only, no PII
                        unmasking)           visibility, real Azure
                                              network access)
```

This is **Orel Server** — it owns all PII masking/unmasking (Presidio,
AES-256-GCM, OCR PDF masking) and is what SmartOps calls (`POST
/public/generate-credit-appraisal`, unchanged contract). It has **no direct
Azure network access** — masked payloads/PDFs go to **CDB Local Server**
instead, a separate codebase (`../cdb-local-server`, sibling folder in this
same git repo) deployed on its **own host**, the only component that calls
Azure directly. Azure's response comes back through CDB Local Server to Orel
Server, which unmasks it and returns the result to SmartOps — synchronous
end-to-end, one extra relay hop inserted into what was previously a direct
Orel->Azure call.

CDB Local Server holds no PII, no masking logic, no database (Postgres/Redis)
— it only ever sees already-masked content, so it needs none of that. It has
its own requirements.txt (no PaddleOCR/Presidio/spacy) for a much smaller
image and a smaller trust footprint at the point closest to Azure. See
`../cdb-local-server/PRODUCTION_CHECKLIST.md` for its own setup.

**Running locally (non-Docker):**
```bash
./.venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000   # Orel Server
```
CDB Local Server is a separate deployable — run it from its own folder (see
its own checklist). This repo needs `endpoints.env` present with
`CDB_LOCAL_SERVER_URL` pointed at wherever CDB Local Server is actually
reachable — `http://127.0.0.1:8100` if running both on one machine for local
testing, or its real host:port once deployed separately.

**Inter-hop auth (Orel Server -> CDB Local Server):** `require_api_key` is
enabled directly on CDB Local Server's relay route (not commented out, since
this hop's only caller — Orel Server — is known and controlled by the same
team). Set `CDB_LOCAL_SERVER_API_KEY` in `endpoints.env` to a real secret;
Orel Server sends it as `X-API-Key` on every call. It must match one of the
values in CDB Local Server's own `CDB_LOCAL_SERVER_API_KEYS`.

## Multi-replica PaddleOCR model cache

`docker-compose.yml` has a one-shot `model-init` service that downloads the
PaddleOCR model weights into the shared `paddleocr_models` volume *before*
any `app1`/`app2`/`app3` replica starts (`depends_on: model-init: condition:
service_completed_successfully`). This was added after directly observing 3
replicas race to populate the same empty volume on a clean deploy — they
corrupted each other's downloads and all failed their healthcheck. Confirmed
fixed with a full clean `docker compose down -v && up -d` (all 3 replicas +
nginx came up healthy). If you ever see `FileNotFoundError` on a `.tmp`
paddleocr path in the logs, this is almost certainly `model-init` having been
skipped or having failed silently — check `docker compose logs model-init`.

## Before go-live — env vars that must change

`.env` and `endpoints.env` already exist in this folder (gitignored) with
dev/placeholder values — change these before going live:

| Var | File | Why |
|---|---|---|
| `PII_MASTER_KEY_HEX` | `.env` | Encrypts every PII blob (AES-256-GCM). The placeholder value is public — never use it outside dev/test. Generate a real one: `python -c "import secrets; print(secrets.token_hex(32))"` |
| `AZURE_CALLBACK_TOKEN` | `.env` | Currently empty = the `/api/v1/azure/callback` webhook accepts any caller. Set a real shared secret before relying on the async callback path. |
| `API_KEYS` | `.env` | See below — required before Orel Server's own (currently commented-out) inbound auth does anything. |
| `USE_FAKE_REDIS` | `.env` | Must be `false` in production. `docker-compose.yml` already sets this against the real `redis` service; fake/in-memory Redis loses all state on restart and isn't shared across the 3 app replicas. |
| `POSTGRES_PASSWORD` | `.env` | Used both by the `postgres` container itself and to build `DATABASE_URL` for the app. Never use a placeholder outside dev/test. |
| `CDB_LOCAL_SERVER_URL` | `endpoints.env` | Now points at a genuinely separate host — set it to CDB Local Server's real address/port. |
| `CDB_LOCAL_SERVER_API_KEY` | `endpoints.env` | Authenticates Orel Server to CDB Local Server — see the architecture section above. Never use a placeholder outside dev/test. |

## Turning on API-key auth

`require_api_key` (in `app/api/routes_appraisal.py`) is fully implemented but its
`Depends(require_api_key)` is commented out on every protected route
(`generate_credit_appraisal`, `appraisal_status`, `appraisal_result`, and the
`/failed-requests` admin endpoints) — left that way deliberately until the
API-key handshake with callers (ERP/whoever calls this gateway) is confirmed.

To turn it on:
1. Set `API_KEYS` in `.env` to one or more comma-separated secrets, e.g. `API_KEYS=key-for-erp-1,key-for-erp-2`.
2. Uncomment the `# _auth: None = Depends(require_api_key),` line on each route in `app/api/routes_appraisal.py`.
3. Callers must send `X-API-Key: <one of the configured keys>` on every request to those routes.

Until both steps are done, `require_api_key` no-ops (see `tests/test_security.py` for
the exact behavior), so nothing breaks by deploying with the code merged but
auth not yet enabled.

## Turning on TLS at the nginx edge

Both `nginx/nginx.conf` and `nginx/nginx.docker.conf` have a commented `listen
...ssl` block. To enable: get real certs, mount them (see the commented volume
in `docker-compose.yml`), uncomment the `ssl_certificate`/`ssl_certificate_key`
lines and the `listen` directive, add `"443:443"` to the nginx service's ports.
Not required for the current internal/VPN-only deployment target, but ready to
flip on if this ever needs to accept traffic from outside a trusted network.

## Audit trail, vendor payloads, and masked PDFs: PostgreSQL

The durable audit trail — every pipeline event, the masked request/response
JSON exchanged with the vendor, and the masked bank-statement PDFs
themselves — lives in Postgres now (`app/services/postgres_store.py`), not
on the filesystem. This replaced a `logs/audit.jsonl` + `vendor_exchanges/` +
`masked_pdfs/` filesystem setup that turned out not to survive on every
deployment target: on a second machine this was deployed to for testing, the
audit log simply wasn't there — almost certainly a Docker volume that wasn't
actually mounted/persisted/writable on that host, since the app itself never
deleted `audit.jsonl`. One Postgres volume with a healthcheck (verified the
same way `redis_data`/`paddleocr_models` already are in this compose file)
is more reliable than three separate host-filesystem paths that each have to
be independently correct on every machine this runs on.

`uploads/`, `masked_pdfs/`, and `vendor_exchanges/` on disk are now purely
**transient per-request staging** — written and deleted again within the
same request (raw uploads right after masking; masked PDFs/payloads right
after they're persisted to Postgres). Nothing there is meant to survive a
request, let alone a restart.

**Retention**: rows older than `AUDIT_RETENTION_DAYS` (default 30) are
**actively deleted** by `scripts/cleanup_old_sessions.py`, run on a schedule:
```cron
0 3 * * * cd /path/to/orel-server && ./.venv/bin/python scripts/cleanup_old_sessions.py
```
Run with `--dry-run` first to see what it would remove without changing
anything. This is a hard delete — once a row crosses 30 days it's gone, by
design (the explicit choice made for this project).

### Connecting Postgres — step by step

**Docker Compose path (recommended, zero extra config):**
1. Set `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` in `.env` (leave
   `DATABASE_URL` blank — `docker-compose.yml`'s `x-app` block builds it
   automatically as `postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@postgres:5432/${POSTGRES_DB}`).
2. `docker compose up -d` — the `postgres` service starts with a named
   volume (`postgres_data`) and a healthcheck; every app replica waits on
   `postgres: condition: service_healthy` before starting.
3. Nothing else to do — the schema (`audit_log`, `vendor_exchanges`,
   `masked_pdfs` tables) is created automatically on first app startup
   (`app/main.py`'s startup handler calls `init_schema()`, which is
   idempotent — safe to run against an already-initialized DB too).

**External/existing Postgres instance instead:**
1. Set `DATABASE_URL` directly in `.env`, e.g.
   `DATABASE_URL=postgresql://user:password@your-postgres-host:5432/appraisal`.
   The `POSTGRES_USER`/`POSTGRES_PASSWORD`/`POSTGRES_DB` vars are unused in
   this path (only consumed by the bundled `postgres` container).
2. Remove or don't start the `postgres` service (e.g. `docker compose up -d --scale postgres=0 ...`,
   or just delete that service block if you'll never use the bundled one).
3. Schema still self-creates on first app startup, same as above.

**Verifying it's actually working:**
```bash
curl http://localhost:8080/api/v1/health        # {"status":"ok"} — now checks Postgres too, not just Redis
docker compose exec postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -c "select count(*) from audit_log;"
```
After submitting a request, confirm the trail landed:
```sql
select event, ts from audit_log where reference_id = '<reference_id>' order by ts;
select filename, length(content) from vendor_exchanges where reference_id = '<reference_id>';
select filename, length(content) from masked_pdfs where reference_id = '<reference_id>';
```

**If you ever manually reset/recreate the Postgres volume/database, restart
the app too.** Schema creation (`init_schema()`) only runs once, at app
startup (`app/main.py`) — a long-running app process has no way to notice
that the database underneath it got swapped out or wiped, so it'll start
throwing `relation "audit_log" does not exist` on every write until
restarted. Directly hit this while testing: recreated the Docker Postgres
volume without restarting an already-running app process, got exactly that
error, confirmed a plain restart fixes it (schema recreated idempotently on
the next startup). Not a bug — just something to remember operationally,
same as any app that expects its schema to exist at startup.

## Verifying a deployment

```bash
docker compose up -d postgres                            # needed for the Postgres-backed tests below
python -m pytest tests/ -v                                # unit + integration tests (auto-skips DB tests if unreachable)
docker compose build                                      # confirms Dockerfile/compose are valid
docker compose up -d
curl http://localhost:8080/api/v1/health                  # {"status": "ok"} once redis AND postgres are reachable
python scripts/cleanup_old_sessions.py --dry-run           # confirms the retention job's DB connectivity/schema
```
