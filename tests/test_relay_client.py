import httpx
import pytest

from app.config import Settings
from app.services.relay_client import OutboundRelayClient, VendorCallError


def _settings(**overrides) -> Settings:
    defaults = dict(
        azure_callback_token="",
        echo_azure=False,
        azure_request_timeout_seconds=5.0,
        azure_max_attempts=2,
        azure_retry_backoff_seconds=0.0,
        azure_retry_status_codes=[502, 503, 504],
    )
    defaults.update(overrides)
    return Settings(**defaults)


# --------------------------------------------------------------------------- URL resolution
# Regression coverage for the bug spent isolating live against the vendor: the
# vendor's real endpoint is confirmed (via their own /openapi.json) to be
# /public/generate-credit-appraisal, but the configured base URL is meant to
# carry just the host — the client must append the path itself. Same logic
# now applies to both hops (Orel -> CDB Local Server, CDB Local Server ->
# Azure), so it's tested here independent of which settings object is used.


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://vendor.example", "https://vendor.example/public/generate-credit-appraisal"),
        ("https://vendor.example/", "https://vendor.example/public/generate-credit-appraisal"),
        (
            "https://vendor.example/public/generate-credit-appraisal",
            "https://vendor.example/public/generate-credit-appraisal",
        ),
        (
            "https://vendor.example/public/generate-credit-appraisal/",
            "https://vendor.example/public/generate-credit-appraisal",
        ),
        ("", ""),
    ],
)
def test_url_resolution_appends_path_once(raw, expected):
    client = OutboundRelayClient(base_url=raw, settings=_settings())
    assert client._url == expected


def test_enabled_false_when_url_empty():
    client = OutboundRelayClient(base_url="", settings=_settings())
    assert client.enabled is False


def test_enabled_false_when_echo_azure():
    client = OutboundRelayClient(base_url="https://vendor.example", settings=_settings(echo_azure=True))
    assert client.enabled is False


def test_enabled_true_with_url_and_no_echo():
    client = OutboundRelayClient(base_url="https://vendor.example", settings=_settings())
    assert client.enabled is True


# --------------------------------------------------------------------------- retry/backoff
# monkeypatch.setattr("app.services.relay_client.httpx.AsyncClient", ...) mutates
# the shared httpx module object (it's the same object this file's `import httpx`
# sees too) — so the fake factory must close over the *original* class, or calling
# httpx.AsyncClient(...) from inside it recurses into itself.
_RealAsyncClient = httpx.AsyncClient


class _ScriptedTransport(httpx.AsyncBaseTransport):
    """Replays a fixed sequence of responses/exceptions, one per call."""

    def __init__(self, script):
        self._script = list(script)
        self.calls = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.mark.asyncio
async def test_succeeds_on_first_attempt(monkeypatch):
    transport = _ScriptedTransport([httpx.Response(200, json={"ok": True})])

    def fake_async_client(*args, **kwargs):
        kwargs.pop("timeout", None)
        return _RealAsyncClient(transport=transport)

    monkeypatch.setattr("app.services.relay_client.httpx.AsyncClient", fake_async_client)

    client = OutboundRelayClient(base_url="https://vendor.example", settings=_settings())
    result = await client.generate_credit_appraisal(masked_data={"a": 1}, masked_pdfs=[])
    assert result == {"ok": True}
    assert transport.calls == 1


@pytest.mark.asyncio
async def test_sends_masked_pdf_bytes_as_multipart_files(monkeypatch):
    """Regression coverage for the file-path -> in-memory-bytes switch (PDFs
    now live in Postgres, not on disk) — confirms the actual bytes/filename
    make it into the outbound multipart request."""
    captured: dict = {}

    class _CapturingTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            await request.aread()  # multipart bodies are streamed lazily; must read before .content
            captured["body"] = request.content
            captured["headers"] = request.headers
            return httpx.Response(200, json={"ok": True})

    def fake_async_client(*args, **kwargs):
        return _RealAsyncClient(transport=_CapturingTransport())

    monkeypatch.setattr("app.services.relay_client.httpx.AsyncClient", fake_async_client)

    client = OutboundRelayClient(base_url="https://vendor.example", settings=_settings())
    result = await client.generate_credit_appraisal(
        masked_data={"a": 1},
        masked_pdfs=[("statement_1.pdf", b"%PDF-fake-content")],
    )
    assert result == {"ok": True}
    assert b"statement_1.pdf" in captured["body"]
    assert b"%PDF-fake-content" in captured["body"]
    assert "x-api-key" not in captured["headers"]  # no key configured -> no header sent


@pytest.mark.asyncio
async def test_sends_x_api_key_header_when_configured(monkeypatch):
    """Orel Server authenticates to CDB Local Server this way; CDB Local
    Server itself passes no key when calling Azure (Azure doesn't use this
    scheme) — same client class, different outbound_api_key per hop."""
    captured: dict = {}

    class _CapturingTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            captured["headers"] = request.headers
            return httpx.Response(200, json={"ok": True})

    def fake_async_client(*args, **kwargs):
        return _RealAsyncClient(transport=_CapturingTransport())

    monkeypatch.setattr("app.services.relay_client.httpx.AsyncClient", fake_async_client)

    client = OutboundRelayClient(
        base_url="https://cdb-local-server.internal", settings=_settings(), outbound_api_key="secret-relay-key"
    )
    await client.generate_credit_appraisal(masked_data={"a": 1}, masked_pdfs=[])
    assert captured["headers"]["x-api-key"] == "secret-relay-key"


@pytest.mark.asyncio
async def test_retries_once_on_503_then_succeeds(monkeypatch):
    transport = _ScriptedTransport(
        [httpx.Response(503, text="busy"), httpx.Response(200, json={"ok": True})]
    )

    def fake_async_client(*args, **kwargs):
        return _RealAsyncClient(transport=transport)

    monkeypatch.setattr("app.services.relay_client.httpx.AsyncClient", fake_async_client)

    client = OutboundRelayClient(base_url="https://vendor.example", settings=_settings(azure_max_attempts=2))
    result = await client.generate_credit_appraisal(masked_data={"a": 1}, masked_pdfs=[])
    assert result == {"ok": True}
    assert transport.calls == 2


@pytest.mark.asyncio
async def test_does_not_retry_4xx(monkeypatch):
    transport = _ScriptedTransport([httpx.Response(422, text="bad request")])

    def fake_async_client(*args, **kwargs):
        return _RealAsyncClient(transport=transport)

    monkeypatch.setattr("app.services.relay_client.httpx.AsyncClient", fake_async_client)

    client = OutboundRelayClient(base_url="https://vendor.example", settings=_settings(azure_max_attempts=3))
    with pytest.raises(VendorCallError) as exc_info:
        await client.generate_credit_appraisal(masked_data={"a": 1}, masked_pdfs=[])
    assert transport.calls == 1
    assert exc_info.value.status_code == 422


@pytest.mark.asyncio
async def test_exhausts_retries_on_repeated_503(monkeypatch):
    transport = _ScriptedTransport([httpx.Response(503, text="busy"), httpx.Response(503, text="busy")])

    def fake_async_client(*args, **kwargs):
        return _RealAsyncClient(transport=transport)

    monkeypatch.setattr("app.services.relay_client.httpx.AsyncClient", fake_async_client)

    client = OutboundRelayClient(base_url="https://vendor.example", settings=_settings(azure_max_attempts=2))
    with pytest.raises(VendorCallError) as exc_info:
        await client.generate_credit_appraisal(masked_data={"a": 1}, masked_pdfs=[])
    assert transport.calls == 2
    assert exc_info.value.attempts == 2
    assert exc_info.value.status_code == 503


@pytest.mark.asyncio
async def test_disabled_client_returns_none(monkeypatch):
    client = OutboundRelayClient(base_url="", settings=_settings())
    result = await client.generate_credit_appraisal(masked_data={"a": 1}, masked_pdfs=[])
    assert result is None
