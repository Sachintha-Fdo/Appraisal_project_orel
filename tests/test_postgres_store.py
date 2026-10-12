"""
Requires a real, reachable Postgres — e.g. `docker compose up -d postgres`
(published on localhost:5432 for host-based dev/testing) or any Postgres
matching the DATABASE_URL in .env. The whole module is skipped, not failed,
if none is reachable, so the rest of the suite stays runnable without Docker.
"""

import uuid

import psycopg
import pytest

from app.config import Settings, get_settings
from app.services.postgres_store import PostgresAuditStore

_settings = get_settings()

try:
    with psycopg.connect(_settings.database_url or "postgresql://", connect_timeout=2) as _conn:
        pass
except Exception as exc:
    pytest.skip(f"Postgres not reachable ({exc}); skipping test_postgres_store.py", allow_module_level=True)


@pytest.fixture
def store():
    s = PostgresAuditStore(_settings)
    s.init_schema()
    yield s
    s.close()


def _ref() -> str:
    return f"test-{uuid.uuid4().hex[:12]}"


def test_ping(store):
    assert store.ping() is True


def test_write_audit_event(store):
    ref = _ref()
    store.write(event="submit_received", reference_id=ref, details={"pdfs": 2})
    with psycopg.connect(_settings.database_url) as conn:
        row = conn.execute(
            "SELECT event, reference_id, details FROM audit_log WHERE reference_id = %s", (ref,)
        ).fetchone()
    assert row is not None
    assert row[0] == "submit_received"
    assert row[1] == ref
    assert row[2] == {"pdfs": 2}


def test_vendor_exchange_roundtrip(store):
    ref = _ref()
    store.save_vendor_exchange(reference_id=ref, filename="masked_request.json", content='{"a": 1}')
    assert store.get_vendor_exchange(reference_id=ref, filename="masked_request.json") == '{"a": 1}'


def test_vendor_exchange_missing_returns_none(store):
    assert store.get_vendor_exchange(reference_id=_ref(), filename="nope.json") is None


def test_vendor_exchange_upsert_overwrites(store):
    ref = _ref()
    store.save_vendor_exchange(reference_id=ref, filename="masked_request.json", content="first")
    store.save_vendor_exchange(reference_id=ref, filename="masked_request.json", content="second")
    assert store.get_vendor_exchange(reference_id=ref, filename="masked_request.json") == "second"


def test_masked_pdfs_roundtrip(store):
    ref = _ref()
    store.save_masked_pdf(reference_id=ref, filename="statement_1.pdf", content=b"%PDF-1")
    store.save_masked_pdf(reference_id=ref, filename="statement_2.pdf", content=b"%PDF-2")
    pdfs = store.get_masked_pdfs(reference_id=ref)
    assert pdfs == [("statement_1.pdf", b"%PDF-1"), ("statement_2.pdf", b"%PDF-2")]


def test_masked_pdfs_missing_reference_returns_empty(store):
    assert store.get_masked_pdfs(reference_id=_ref()) == []


def test_purge_older_than_deletes_old_keeps_new(store):
    old_ref, new_ref = _ref(), _ref()
    with psycopg.connect(_settings.database_url) as conn:
        # Backdate one row per table for old_ref so it's older than the cutoff.
        conn.execute(
            "INSERT INTO audit_log (ts, event, reference_id, details) VALUES (now() - interval '31 days', 'x', %s, '{}')",
            (old_ref,),
        )
        conn.execute(
            "INSERT INTO vendor_exchanges (reference_id, filename, content, created_at) "
            "VALUES (%s, 'f.json', 'x', now() - interval '31 days')",
            (old_ref,),
        )
        conn.execute(
            "INSERT INTO masked_pdfs (reference_id, filename, content, created_at) "
            "VALUES (%s, 'f.pdf', %s, now() - interval '31 days')",
            (old_ref, b"x"),
        )
    store.write(event="y", reference_id=new_ref, details={})
    store.save_vendor_exchange(reference_id=new_ref, filename="f.json", content="y")
    store.save_masked_pdf(reference_id=new_ref, filename="f.pdf", content=b"y")

    counts = store.purge_older_than(30)
    assert counts == {"audit_log": 1, "vendor_exchanges": 1, "masked_pdfs": 1}

    assert store.get_vendor_exchange(reference_id=old_ref, filename="f.json") is None
    assert store.get_vendor_exchange(reference_id=new_ref, filename="f.json") == "y"
    assert store.get_masked_pdfs(reference_id=old_ref) == []
    assert store.get_masked_pdfs(reference_id=new_ref) == [("f.pdf", b"y")]
