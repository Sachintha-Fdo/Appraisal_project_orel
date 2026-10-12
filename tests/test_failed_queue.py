from app.config import Settings
from app.services.failed_queue import FailedRequestQueue


def _queue() -> FailedRequestQueue:
    return FailedRequestQueue(Settings(use_fake_redis=True, failed_queue_key="test_failed", failed_queue_max_len=5))


def test_add_and_get():
    q = _queue()
    q.add(reference_id="ref-1", stage="vendor_call", reason="timeout", http_status=504, attempts=2)
    record = q.get("ref-1")
    assert record["reference_id"] == "ref-1"
    assert record["stage"] == "vendor_call"
    assert record["http_status"] == 504


def test_get_missing_returns_none():
    assert _queue().get("nope") is None


def test_list_returns_most_recent_first():
    q = _queue()
    q.add(reference_id="ref-1", stage="processing", reason="a")
    q.add(reference_id="ref-2", stage="processing", reason="b")
    records = q.list(limit=10)
    assert [r["reference_id"] for r in records] == ["ref-2", "ref-1"]


def test_repeat_failure_moves_to_front_not_duplicated():
    q = _queue()
    q.add(reference_id="ref-1", stage="processing", reason="a")
    q.add(reference_id="ref-2", stage="processing", reason="b")
    q.add(reference_id="ref-1", stage="processing", reason="a-retry")
    records = q.list(limit=10)
    assert [r["reference_id"] for r in records] == ["ref-1", "ref-2"]
    assert q.count() == 2


def test_remove():
    q = _queue()
    q.add(reference_id="ref-1", stage="processing", reason="a")
    q.remove("ref-1")
    assert q.get("ref-1") is None
    assert q.list(limit=10) == []


def test_max_len_trims_oldest():
    q = _queue()  # failed_queue_max_len=5
    for i in range(7):
        q.add(reference_id=f"ref-{i}", stage="processing", reason="x")
    records = q.list(limit=10)
    assert len(records) == 5
    assert [r["reference_id"] for r in records] == ["ref-6", "ref-5", "ref-4", "ref-3", "ref-2"]
