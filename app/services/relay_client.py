import asyncio
import io
import json
import logging
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)


class VendorCallError(Exception):
    

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        attempts: int = 1,
        is_timeout: bool = False,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.attempts = attempts
        self.is_timeout = is_timeout


def _classify(exc: Exception, retry_status_codes: set[int]) -> tuple[bool, Optional[int], bool]:
    
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code in retry_status_codes, code, False
    if isinstance(exc, httpx.TimeoutException):
        return True, None, True
    if isinstance(exc, httpx.TransportError):
        # Connection refused/reset, DNS failure, etc. — worth one retry.
        return True, None, False
    return False, None, False


def _describe_failure(exc: Optional[Exception]) -> str:
    if exc is None:
        return "unknown error"
    if isinstance(exc, httpx.HTTPStatusError):
        snippet = exc.response.text[:500]
        return f"HTTP {exc.response.status_code} from upstream: {snippet}" if snippet else f"HTTP {exc.response.status_code} from upstream"
    if isinstance(exc, httpx.TimeoutException):
        return f"Upstream request timed out: {exc}"
    if isinstance(exc, httpx.TransportError):
        return f"Network/connection error calling upstream: {exc}"
    return f"{type(exc).__name__}: {exc}"


class OutboundRelayClient:
    
    _APPRAISAL_PATH = "/public/generate-credit-appraisal"

    def __init__(self, *, base_url: str, settings: Any, outbound_api_key: str = ""):
        base = (base_url or "").strip().rstrip("/")
        if base and not base.endswith(self._APPRAISAL_PATH):
            base = base + self._APPRAISAL_PATH
        self._url = base
        self._settings = settings
        self._outbound_api_key = (outbound_api_key or "").strip()

    @property
    def enabled(self) -> bool:
        return bool(self._url) and not self._settings.echo_azure

    async def generate_credit_appraisal(
        self,
        *,
        masked_data: dict[str, Any],
        masked_pdfs: list[tuple[str, bytes]],
    ) -> Any:
        """
        POST the masked payload to `base_url`'s generate-credit-appraisal
        endpoint and return its response body as-is (dict, list, or plain
        string) so the caller can pass it along/unmask whatever shape comes
        back.

        Raises VendorCallError if all attempts fail.
        """
        if not self.enabled:
            return None

        max_attempts = max(1, self._settings.azure_max_attempts)
        backoff_seconds = self._settings.azure_retry_backoff_seconds
        retry_status_codes = set(self._settings.azure_retry_status_codes)

        form_data = {"data": json.dumps(masked_data, ensure_ascii=False)}
        headers = {"X-API-Key": self._outbound_api_key} if self._outbound_api_key else None
        timeout = httpx.Timeout(self._settings.azure_request_timeout_seconds, connect=30.0)

        last_exc: Optional[Exception] = None
        last_status: Optional[int] = None
        last_is_timeout = False
        attempt = 0

        for attempt in range(1, max_attempts + 1):
            try:
                files: list[tuple[str, tuple[str, Any, str]]] = [
                    ("file", (name, io.BytesIO(content), "application/pdf")) for name, content in masked_pdfs
                ]

                async with httpx.AsyncClient(timeout=timeout) as client:
                    resp = await client.post(self._url, data=form_data, files=files or None, headers=headers)
                    resp.raise_for_status()
                    if not resp.content:
                        return None
                    try:
                        return resp.json()
                    except Exception:
                        return resp.text
            except Exception as e:
                last_exc = e
                is_retryable, status_code, is_timeout = _classify(e, retry_status_codes)
                last_status, last_is_timeout = status_code, is_timeout
                logger.warning(
                    "Outbound call attempt %d/%d failed (status=%s, timeout=%s): %s",
                    attempt,
                    max_attempts,
                    status_code,
                    is_timeout,
                    e,
                )
                if attempt < max_attempts and is_retryable:
                    await asyncio.sleep(backoff_seconds)
                    continue
                break

        raise VendorCallError(
            _describe_failure(last_exc),
            status_code=last_status,
            attempts=attempt,
            is_timeout=last_is_timeout,
        ) from last_exc
