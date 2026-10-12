from app.config import Settings
from app.services.redis_store import SessionStore


def _store() -> SessionStore:
    return SessionStore(Settings(use_fake_redis=True, pii_redis_ttl_seconds=60))


def test_ping():
    assert _store().ping() is True


def test_pii_blob_roundtrip():
    store = _store()
    store.save_pii_blob("ref-1", "tok-1", b"ciphertext-bytes")
    assert store.get_pii_blob("ref-1", "tok-1") == b"ciphertext-bytes"


def test_missing_pii_blob_returns_none():
    store = _store()
    assert store.get_pii_blob("ref-1", "nope") is None


def test_session_meta_roundtrip():
    store = _store()
    store.set_session_meta("ref-1", {"status": "masked"})
    assert store.get_session_meta("ref-1") == {"status": "masked"}


def test_missing_session_meta_returns_none():
    assert _store().get_session_meta("nope") is None


def test_update_session_meta_merges():
    store = _store()
    store.set_session_meta("ref-1", {"status": "masked", "pdf_count": 2})
    store.update_session_meta("ref-1", {"status": "completed"})
    assert store.get_session_meta("ref-1") == {"status": "completed", "pdf_count": 2}
