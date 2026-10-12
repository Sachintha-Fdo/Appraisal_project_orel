import pytest
from fastapi import HTTPException

from app.api.routes_appraisal import require_api_key
from app.config import Settings
from app.container import build_container


def _container(api_keys):
    settings = Settings(use_fake_redis=True, api_keys=api_keys, pii_master_key_hex="0" * 64)
    return build_container(settings)


def test_noop_when_no_api_keys_configured():
    c = _container([])
    # No key supplied at all — still allowed, since auth isn't turned on yet.
    require_api_key(x_api_key=None, c=c)


def test_rejects_missing_key_when_configured():
    c = _container(["secret-key-1"])
    with pytest.raises(HTTPException) as exc_info:
        require_api_key(x_api_key=None, c=c)
    assert exc_info.value.status_code == 401


def test_rejects_wrong_key_when_configured():
    c = _container(["secret-key-1"])
    with pytest.raises(HTTPException) as exc_info:
        require_api_key(x_api_key="wrong", c=c)
    assert exc_info.value.status_code == 401


def test_accepts_correct_key():
    c = _container(["secret-key-1", "secret-key-2"])
    require_api_key(x_api_key="secret-key-2", c=c)  # must not raise
