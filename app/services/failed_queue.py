import json
from datetime import datetime, timezone
from typing import Any, Optional, Union

import redis

from app.config import Settings


class FailedRequestQueue:
    """
    Durable record of appraisal requests that did not complete successfully —
    vendor call exhausted its retries, PDF masking timed out, or an unexpected
    pipeline error broke the flow. Kept separate from SessionStore's per-request
    TTL'd metadata (which expires and is keyed only by reference_id) so
    operators have one place to answer "what's currently broken" and can
    replay a failure after fixing whatever caused it.
    """

    def __init__(self, settings: Settings):
        if settings.use_fake_redis:
            import fakeredis

            self._r: Union[redis.Redis, Any] = fakeredis.FakeRedis(decode_responses=False)
        else:
            self._r = redis.Redis.from_url(settings.redis_url, decode_responses=False)
        self._list_key = f"{settings.failed_queue_key}:list".encode("utf-8")
        self._hash_key = f"{settings.failed_queue_key}:by_ref".encode("utf-8")
        self._max_len = settings.failed_queue_max_len

    def add(
        self,
        *,
        reference_id: str,
        stage: str,
        reason: str,
        http_status: Optional[int] = None,
        attempts: int = 1,
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        record = {
            "reference_id": reference_id,
            "stage": stage,
            "reason": reason,
            "http_status": http_status,
            "attempts": attempts,
            "details": details or {},
            "failed_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        blob = json.dumps(record, ensure_ascii=False).encode("utf-8")
        ref_key = reference_id.encode("utf-8")
        pipe = self._r.pipeline()
        pipe.hset(self._hash_key, ref_key, blob)
        # Move to the front on repeat failures for the same reference_id, rather
        # than leaving a stale position from its first failure.
        pipe.lrem(self._list_key, 0, ref_key)
        pipe.lpush(self._list_key, ref_key)
        pipe.ltrim(self._list_key, 0, self._max_len - 1)
        pipe.execute()

    def get(self, reference_id: str) -> Optional[dict]:
        raw = self._r.hget(self._hash_key, reference_id.encode("utf-8"))
        if not raw:
            return None
        return json.loads(raw.decode("utf-8"))

    def remove(self, reference_id: str) -> None:
        ref_key = reference_id.encode("utf-8")
        pipe = self._r.pipeline()
        pipe.hdel(self._hash_key, ref_key)
        pipe.lrem(self._list_key, 0, ref_key)
        pipe.execute()

    def list(self, limit: int = 100) -> list[dict]:
        ref_ids = self._r.lrange(self._list_key, 0, max(limit, 1) - 1)
        if not ref_ids:
            return []
        raws = self._r.hmget(self._hash_key, ref_ids)
        return [json.loads(raw.decode("utf-8")) for raw in raws if raw]

    def count(self) -> int:
        return self._r.hlen(self._hash_key)
