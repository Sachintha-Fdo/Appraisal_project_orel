from typing import Any, Optional

from psycopg.types.json import Json
from psycopg_pool import ConnectionPool

from app.config import Settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    id BIGSERIAL PRIMARY KEY,
    ts TIMESTAMPTZ NOT NULL DEFAULT now(),
    event TEXT NOT NULL,
    reference_id TEXT NOT NULL,
    details JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_audit_log_reference_id ON audit_log (reference_id);
CREATE INDEX IF NOT EXISTS idx_audit_log_ts ON audit_log (ts);

CREATE TABLE IF NOT EXISTS vendor_exchanges (
    id BIGSERIAL PRIMARY KEY,
    reference_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (reference_id, filename)
);
CREATE INDEX IF NOT EXISTS idx_vendor_exchanges_reference_id ON vendor_exchanges (reference_id);
CREATE INDEX IF NOT EXISTS idx_vendor_exchanges_created_at ON vendor_exchanges (created_at);

CREATE TABLE IF NOT EXISTS masked_pdfs (
    id BIGSERIAL PRIMARY KEY,
    reference_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    content BYTEA NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (reference_id, filename)
);
CREATE INDEX IF NOT EXISTS idx_masked_pdfs_reference_id ON masked_pdfs (reference_id);
CREATE INDEX IF NOT EXISTS idx_masked_pdfs_created_at ON masked_pdfs (created_at);
"""


class PostgresAuditStore:
    """
    Durable, machine-independent home for the audit trail: every pipeline
    event, the masked request/response payloads exchanged with the vendor,
    and the masked bank-statement PDFs themselves. Replaces the old
    filesystem-based logs/audit.jsonl + vendor_exchanges/ + masked_pdfs/
    (which turned out not to survive on every deployment target — one Docker
    volume with a healthcheck, verified the same way as redis_data, is more
    reliable than three separate host-filesystem paths).

    ConnectionPool's default open behavior is non-blocking — construction
    kicks off background connection attempts and returns immediately even if
    Postgres is completely unreachable, so build_container() stays safe to
    call in tests that don't exercise Postgres. init_schema() is the first
    call that actually waits on a connection — called explicitly from
    app/main.py's startup handler, where a connectivity failure should
    legitimately crash startup (same posture as the existing
    PII_MASTER_KEY_HEX validation).
    """

    def __init__(self, settings: Settings):
        self._pool = ConnectionPool(settings.database_url or "postgresql://", min_size=0, max_size=10, open=True)
        self._retention_days = settings.audit_retention_days

    def init_schema(self) -> None:
        with self._pool.connection() as conn:
            conn.execute(_SCHEMA)

    def ping(self) -> bool:
        with self._pool.connection() as conn:
            conn.execute("SELECT 1")
        return True

    def write(
        self,
        *,
        event: str,
        reference_id: str,
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO audit_log (event, reference_id, details) VALUES (%s, %s, %s)",
                (event, reference_id, Json(details or {})),
            )

    def save_vendor_exchange(self, *, reference_id: str, filename: str, content: str) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO vendor_exchanges (reference_id, filename, content)
                VALUES (%s, %s, %s)
                ON CONFLICT (reference_id, filename)
                DO UPDATE SET content = EXCLUDED.content, created_at = now()
                """,
                (reference_id, filename, content),
            )

    def get_vendor_exchange(self, *, reference_id: str, filename: str) -> Optional[str]:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT content FROM vendor_exchanges WHERE reference_id = %s AND filename = %s",
                (reference_id, filename),
            ).fetchone()
        return row[0] if row else None

    def save_masked_pdf(self, *, reference_id: str, filename: str, content: bytes) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO masked_pdfs (reference_id, filename, content)
                VALUES (%s, %s, %s)
                ON CONFLICT (reference_id, filename)
                DO UPDATE SET content = EXCLUDED.content, created_at = now()
                """,
                (reference_id, filename, content),
            )

    def get_masked_pdfs(self, *, reference_id: str) -> list[tuple[str, bytes]]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT filename, content FROM masked_pdfs WHERE reference_id = %s ORDER BY id",
                (reference_id,),
            ).fetchall()
        return [(filename, bytes(content)) for filename, content in rows]

    def purge_older_than(self, days: int) -> dict[str, int]:
        """Deletes rows older than `days` from all three tables. Returns per-table row counts."""
        counts: dict[str, int] = {}
        with self._pool.connection() as conn:
            for table, ts_column in (
                ("audit_log", "ts"),
                ("vendor_exchanges", "created_at"),
                ("masked_pdfs", "created_at"),
            ):
                cur = conn.execute(
                    f"DELETE FROM {table} WHERE {ts_column} < now() - (%s * interval '1 day')",
                    (days,),
                )
                counts[table] = cur.rowcount
        return counts

    def close(self) -> None:
        self._pool.close()
