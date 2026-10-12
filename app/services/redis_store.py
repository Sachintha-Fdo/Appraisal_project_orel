import json
from typing import Any, Optional, Union

import redis

from app.config import Settings


class SessionStore:
    def __init__(self, settings: Settings):
        if settings.use_fake_redis:
            import fakeredis

            self._r: Union[redis.Redis, Any] = fakeredis.FakeRedis(decode_responses=False)
        else:
            self._r = redis.Redis.from_url(settings.redis_url, decode_responses=False)
        self._ttl = settings.pii_redis_ttl_seconds

    def ping(self) -> bool:
        return bool(self._r.ping())

    def _pii_key(self, reference_id: str, token_id: str) -> str:
        return f"pii:{reference_id}:{token_id}".encode("utf-8")

    def _meta_key(self, reference_id: str) -> str:
        return f"session:{reference_id}".encode("utf-8")

    def save_pii_blob(self, reference_id: str, token_id: str, blob: bytes) -> None:
        self._r.setex(self._pii_key(reference_id, token_id), self._ttl, blob)

    def get_pii_blob(self, reference_id: str, token_id: str) -> Optional[bytes]:
        v = self._r.get(self._pii_key(reference_id, token_id))
        return v if v is not None else None

    def set_session_meta(self, reference_id: str, meta: dict) -> None:
        self._r.setex(self._meta_key(reference_id), self._ttl, json.dumps(meta).encode("utf-8"))

    def get_session_meta(self, reference_id: str) -> Optional[dict]:
        raw = self._r.get(self._meta_key(reference_id))
        if not raw:
            return None
        return json.loads(raw.decode("utf-8"))

    def update_session_meta(self, reference_id: str, patch: dict) -> None:
        cur = self.get_session_meta(reference_id) or {}
        cur.update(patch)
        self.set_session_meta(reference_id, cur)
