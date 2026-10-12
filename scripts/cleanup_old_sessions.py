#!/usr/bin/env python3
"""
Retention cleanup for the Postgres-backed audit trail (audit_log,
vendor_exchanges, masked_pdfs — see app/services/postgres_store.py). All
three only ever hold PII-masked (⟦PII:token⟧ placeholder) content or event
metadata, never raw PII, but still grow unbounded without this. Deletes rows
older than AUDIT_RETENTION_DAYS (default 30).

Intended to run on a schedule (cron/Task Scheduler), not in-process:
  0 3 * * * cd /path/to/app && ./.venv/bin/python scripts/cleanup_old_sessions.py

Usage:
  python scripts/cleanup_old_sessions.py [--dry-run]
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))

from app.config import get_settings  # noqa: E402
from app.services.postgres_store import PostgresAuditStore  # noqa: E402


def _load_proj_dotenv() -> None:
    import os

    p = PROJ / ".env"
    if not p.is_file():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


def main() -> int:
    _load_proj_dotenv()
    dry_run = "--dry-run" in sys.argv

    settings = get_settings()
    store = PostgresAuditStore(settings)
    store.init_schema()  # idempotent; safe even on a totally fresh DB

    if dry_run:
        print(f"[dry-run] would purge rows older than {settings.audit_retention_days} days "
              f"from audit_log, vendor_exchanges, masked_pdfs (no changes made).")
        return 0

    counts = store.purge_older_than(settings.audit_retention_days)
    total = sum(counts.values())
    for table, n in counts.items():
        print(f"  {table}: removed {n} row{'s' if n != 1 else ''}")
    print(f"Removed {total} row{'s' if total != 1 else ''} total, older than {settings.audit_retention_days} days.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
