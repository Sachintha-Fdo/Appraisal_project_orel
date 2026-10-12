import asyncio
import json
import os
import shutil
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from app.container import AppContainer
from app.models.appraisal_schema import CreditAppraisalData
from app.services.relay_client import VendorCallError
from app.services.presidio_masker import new_reference_id

router = APIRouter(prefix="/api/v1")
public_router = APIRouter()


def get_container(request: Request) -> AppContainer:
    c = getattr(request.app.state, "container", None)
    if c is None:
        raise HTTPException(status_code=503, detail="Service not ready")
    return c


def require_api_key(
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
    c: AppContainer = Depends(get_container),
) -> None:
    """
    Shared-secret auth for callers. No-ops when API_KEYS is unset, so it's
    safe to leave wired up without breaking anything until it's turned on
    (see the commented-out Depends(require_api_key) below on each protected
    route — uncomment once the API-key handshake is confirmed with callers).
    """
    if not c.settings.api_keys:
        return
    if not x_api_key or x_api_key not in c.settings.api_keys:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


def _serialize(data: Any) -> str:
    """Text form of a masked (no real PII) request/response snapshot, for Postgres storage."""
    if isinstance(data, (dict, list)):
        return json.dumps(data, indent=2, ensure_ascii=False)
    return "" if data is None else str(data)


class AzureCallbackBody(BaseModel):
    reference_id: str
    processed_masked_payload: Any = Field(
        ...,
        description=(
            "Vendor response JSON/text that still contains ⟦PII:token⟧ placeholders. "
            "Any shape is accepted: dict, list, or plain string."
        ),
    )


@router.get("/health")
def health(c: AppContainer = Depends(get_container)) -> dict[str, str]:
    try:
        c.store.ping()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Redis unreachable: {e}") from e
    try:
        c.postgres.ping()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Postgres unreachable: {e}") from e
    return {"status": "ok"}


@public_router.post("/public/generate-credit-appraisal")
async def generate_credit_appraisal(
    c: AppContainer = Depends(get_container),
    data: str = Form(..., description="JSON string matching the vendor's credit appraisal request schema"),
    file: Optional[list[UploadFile]] = File(None),
    # _auth: None = Depends(require_api_key),  # Uncomment once the API-key handshake is confirmed with callers
) -> dict[str, Any]:
    """
    Synchronous pipeline: accept the requester's (SmartOps/ERP) data + bank
    statement PDFs, mask all PII (JSON fields + PDF content), forward the
    masked request to CDB Local Server — which relays it to the vendor's
    Azure AI Foundry appraisal generator, since this server has no direct
    Azure network access — then unmask whatever shape comes back and hand
    the finished appraisal back to the requester.
    """
    files = file or []
    if len(files) > 10:
        raise HTTPException(status_code=400, detail="Maximum 10 bank statement PDFs")
    if not files:
        raise HTTPException(status_code=400, detail="At least one bank statement PDF (field 'file') is required")

    try:
        parsed = json.loads(data)
        appraisal = CreditAppraisalData.model_validate(parsed)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid data JSON: {e}") from e

    reference_id = new_reference_id()
    upload_dir: Optional[str] = None
    try:
        body = appraisal.model_dump(mode="json", exclude_none=True)

        os.makedirs(c.settings.uploads_dir, exist_ok=True)
        upload_dir = os.path.join(c.settings.uploads_dir, reference_id)
        os.makedirs(upload_dir, exist_ok=True)

        saved_pdfs: list[str] = []
        for f in files:
            if not f.filename:
                continue
            header = f.file.read(4)
            f.file.seek(0)
            if header != b"%PDF":
                raise HTTPException(status_code=400, detail=f"'{f.filename}' is not a valid PDF file")
            dest = os.path.join(upload_dir, Path(f.filename).name)
            with open(dest, "wb") as out:
                shutil.copyfileobj(f.file, out)
            saved_pdfs.append(dest)

        c.audit.write(
            event="submit_received",
            reference_id=reference_id,
            details={"pdfs": len(saved_pdfs), "bank": body.get("bank"), "loan_number": body.get("loan_number")},
        )

        masked_payload = c.masker.mask_value(body, reference_id)
        c.postgres.save_vendor_exchange(
            reference_id=reference_id, filename="masked_request.json", content=_serialize(masked_payload)
        )

        masked_pdfs: list[tuple[str, bytes]] = []
        if saved_pdfs:
            # Run off the event loop: PaddleOCR-based masking is CPU-bound and can take
            # tens of seconds per PDF. Without this, one slow/large upload would block
            # every other request (health checks included) for the whole process.
            try:
                masked_pdfs = await asyncio.wait_for(
                    run_in_threadpool(c.pdf_service.mask_pdfs, saved_pdfs, reference_id, c.postgres),
                    timeout=c.settings.pdf_mask_timeout_seconds,
                )
            except asyncio.TimeoutError as e:
                reason = f"PDF masking exceeded {c.settings.pdf_mask_timeout_seconds:.0f}s"
                c.store.update_session_meta(reference_id, {"status": "pdf_mask_timeout", "error": reason})
                c.audit.write(
                    event="pdf_mask_timeout",
                    reference_id=reference_id,
                    details={"pdf_count": len(saved_pdfs), "timeout_seconds": c.settings.pdf_mask_timeout_seconds},
                )
                c.failed_queue.add(
                    reference_id=reference_id,
                    stage="pdf_masking",
                    reason=reason,
                    http_status=504,
                    details={"pdf_count": len(saved_pdfs)},
                )
                raise HTTPException(
                    status_code=504,
                    detail=f"{reason}; try fewer or smaller files",
                ) from e

        # Raw uploads only exist to feed mask_pdfs (see app/services/pdf_service.py) —
        # nothing downstream (including manual retry) reads them again, so the
        # unmasked PII shouldn't linger on disk any longer than necessary.
        shutil.rmtree(upload_dir, ignore_errors=True)

        c.store.set_session_meta(reference_id, {"status": "masked"})
        c.audit.write(event="masked", reference_id=reference_id, details={"pdf_count": len(masked_pdfs)})

        if c.settings.echo_azure:
            c.postgres.save_vendor_exchange(
                reference_id=reference_id, filename="vendor_response_raw.json", content=_serialize(masked_payload)
            )
            unmasked = c.masker.unmask_value(masked_payload, reference_id)
            c.store.update_session_meta(reference_id, {"status": "completed", "result": unmasked})
            c.audit.write(event="echo_unmasked", reference_id=reference_id, details={})
            return {
                "reference_id": reference_id,
                "status": "completed",
                "mode": "echo_azure",
                "masked_payload": masked_payload,
                "result": unmasked,
            }

        if not c.relay.enabled:
            c.store.update_session_meta(reference_id, {"status": "masked_local_no_azure"})
            return {
                "reference_id": reference_id,
                "status": "masked_local_no_azure",
                "message": "Set CDB_LOCAL_SERVER_URL (endpoints.env) or ECHO_AZURE=true to exercise the full pipeline",
                "masked_payload": masked_payload,
            }

        try:
            vendor_response = await c.relay.generate_credit_appraisal(
                masked_data=masked_payload,
                masked_pdfs=masked_pdfs,
            )
        except VendorCallError as e:
            # OutboundRelayClient already retried transient failures (network errors,
            # timeouts, 502/503/504) once before giving up — this is the final,
            # exhausted-retries failure. Record the reason so it isn't just a
            # vanished request, and surface it in the failed-requests queue for
            # operators to inspect or manually replay via /failed-requests/{id}/retry.
            c.store.update_session_meta(
                reference_id,
                {"status": "vendor_call_failed", "error": str(e), "attempts": e.attempts},
            )
            c.audit.write(
                event="vendor_call_failed",
                reference_id=reference_id,
                details={"error": str(e), "attempts": e.attempts, "status_code": e.status_code},
            )
            c.failed_queue.add(
                reference_id=reference_id,
                stage="vendor_call",
                reason=str(e),
                http_status=e.status_code,
                attempts=e.attempts,
                details={"is_timeout": e.is_timeout},
            )
            status_code = 504 if e.is_timeout else 502
            raise HTTPException(
                status_code=status_code,
                detail=(
                    f"Vendor appraisal generation failed after {e.attempts} attempt(s) "
                    f"(reference_id={reference_id}): {e}"
                ),
            ) from e

        c.postgres.save_vendor_exchange(
            reference_id=reference_id, filename="vendor_response_raw.json", content=_serialize(vendor_response)
        )
        unmasked = c.masker.unmask_value(vendor_response, reference_id)
        c.store.update_session_meta(reference_id, {"status": "completed", "result": unmasked})
        c.audit.write(event="vendor_unmasked", reference_id=reference_id, details={})

        return {
            "reference_id": reference_id,
            "status": "completed",
            "result": unmasked,
        }
    except HTTPException:
        # Already a deliberate, specific error response (timeout, vendor failure, etc.) — pass through.
        # rmtree is a no-op (ignore_errors=True) if the happy-path cleanup above already ran.
        if upload_dir:
            shutil.rmtree(upload_dir, ignore_errors=True)
        raise
    except Exception as e:
        # Catch-all so an unexpected failure anywhere in the pipeline (disk I/O, a
        # masking/unmasking bug, a corrupt PDF, ...) never returns a raw traceback
        # or leaves the session stuck in an ambiguous state.
        if upload_dir:
            shutil.rmtree(upload_dir, ignore_errors=True)
        c.store.update_session_meta(reference_id, {"status": "failed", "error": str(e)})
        c.audit.write(event="processing_failed", reference_id=reference_id, details={"error": str(e)})
        c.failed_queue.add(
            reference_id=reference_id,
            stage="processing",
            reason=str(e),
            http_status=500,
        )
        raise HTTPException(
            status_code=500,
            detail=f"Unexpected error processing appraisal (reference_id={reference_id})",
        ) from e


@router.post("/azure/callback")
async def azure_callback(
    body: AzureCallbackBody,
    c: AppContainer = Depends(get_container),
    x_callback_token: Optional[str] = Header(default=None, alias="X-Callback-Token"),
) -> dict[str, Any]:
    """
    Async fallback: if the vendor ever pushes the generated appraisal back via
    webhook instead of returning it inline, this unmasks and stores it the same
    way, retrievable via GET /api/v1/appraisal/{reference_id}/result.
    """
    expected = (c.settings.azure_callback_token or "").strip()
    if expected:
        token = (x_callback_token or "").strip()
        if token != expected:
            raise HTTPException(status_code=401, detail="Invalid callback token")

    reference_id = body.reference_id
    meta = c.store.get_session_meta(reference_id)
    if not meta:
        raise HTTPException(status_code=404, detail="Unknown reference_id")

    try:
        c.postgres.save_vendor_exchange(
            reference_id=reference_id,
            filename="vendor_response_raw.json",
            content=_serialize(body.processed_masked_payload),
        )

        unmasked = c.masker.unmask_value(body.processed_masked_payload, reference_id)
        c.store.update_session_meta(reference_id, {"status": "completed", "result": unmasked})
        c.audit.write(event="callback_unmasked", reference_id=reference_id, details={})
    except HTTPException:
        raise
    except Exception as e:
        c.store.update_session_meta(reference_id, {"status": "failed", "error": str(e)})
        c.audit.write(event="callback_processing_failed", reference_id=reference_id, details={"error": str(e)})
        c.failed_queue.add(
            reference_id=reference_id,
            stage="callback",
            reason=str(e),
            http_status=500,
        )
        raise HTTPException(
            status_code=500,
            detail=f"Unexpected error processing vendor callback (reference_id={reference_id})",
        ) from e

    return {"reference_id": reference_id, "status": "completed"}


@router.get("/appraisal/{reference_id}/status")
def appraisal_status(
    reference_id: str,
    c: AppContainer = Depends(get_container),
    # _auth: None = Depends(require_api_key),  # Uncomment once the API-key handshake is confirmed with callers
) -> dict[str, Any]:
    meta = c.store.get_session_meta(reference_id)
    if not meta:
        raise HTTPException(status_code=404, detail="Unknown reference_id")
    meta_out = {k: v for k, v in meta.items() if k != "result"}
    return {"reference_id": reference_id, **meta_out}


@router.get("/appraisal/{reference_id}/result")
def appraisal_result(
    reference_id: str,
    c: AppContainer = Depends(get_container),
    # _auth: None = Depends(require_api_key),  # Uncomment once the API-key handshake is confirmed with callers
) -> dict[str, Any]:
    meta = c.store.get_session_meta(reference_id)
    if not meta:
        raise HTTPException(status_code=404, detail="Unknown reference_id")
    if meta.get("status") != "completed" or "result" not in meta:
        raise HTTPException(status_code=409, detail="Result not ready")
    return {"reference_id": reference_id, "result": meta["result"]}


# --------------------------------------------------------------------------- failed-requests admin
# Operator-facing view over FailedRequestQueue: every request that exhausted its
# vendor-call retries, hit a PDF-masking timeout, or broke on an unexpected
# pipeline error ends up here (see the except blocks above), independent of the
# per-session TTL. These are internal/ops endpoints — place them behind your
# internal network boundary or an auth proxy before exposing outside the VPC.


@router.get("/failed-requests")
def list_failed_requests(
    limit: int = 100,
    c: AppContainer = Depends(get_container),
    # _auth: None = Depends(require_api_key),  # Uncomment once the API-key handshake is confirmed with callers
) -> dict[str, Any]:
    records = c.failed_queue.list(limit=limit)
    return {"count": len(records), "total": c.failed_queue.count(), "items": records}


@router.get("/failed-requests/{reference_id}")
def get_failed_request(
    reference_id: str,
    c: AppContainer = Depends(get_container),
    # _auth: None = Depends(require_api_key),  # Uncomment once the API-key handshake is confirmed with callers
) -> dict[str, Any]:
    record = c.failed_queue.get(reference_id)
    if not record:
        raise HTTPException(status_code=404, detail="No failed-request record for this reference_id")
    return record


@router.delete("/failed-requests/{reference_id}")
def dismiss_failed_request(
    reference_id: str,
    c: AppContainer = Depends(get_container),
    # _auth: None = Depends(require_api_key),  # Uncomment once the API-key handshake is confirmed with callers
) -> dict[str, Any]:
    if not c.failed_queue.get(reference_id):
        raise HTTPException(status_code=404, detail="No failed-request record for this reference_id")
    c.failed_queue.remove(reference_id)
    return {"reference_id": reference_id, "status": "dismissed"}


@router.post("/failed-requests/{reference_id}/retry")
async def retry_failed_request(
    reference_id: str,
    c: AppContainer = Depends(get_container),
    # _auth: None = Depends(require_api_key),  # Uncomment once the API-key handshake is confirmed with callers
) -> dict[str, Any]:
    """
    Manually replay a failed vendor call using the masked payload/PDFs already
    persisted for this reference_id (no PII ever needs to be resubmitted since
    only the masked artifacts are replayed). Useful once the underlying vendor
    outage/network issue has cleared.
    """
    if not c.failed_queue.get(reference_id):
        raise HTTPException(status_code=404, detail="No failed-request record for this reference_id")

    # Fetched straight from Postgres by reference_id — no longer depends on
    # Redis session meta still being present, so retry now works for the
    # full 30-day Postgres retention window, not just Redis's shorter TTL.
    masked_request_content = c.postgres.get_vendor_exchange(reference_id=reference_id, filename="masked_request.json")
    if not masked_request_content:
        raise HTTPException(status_code=409, detail="Masked request payload is no longer available for retry")
    masked_payload = json.loads(masked_request_content)
    masked_pdfs = c.postgres.get_masked_pdfs(reference_id=reference_id)

    if not c.relay.enabled:
        raise HTTPException(status_code=409, detail="Vendor endpoint not configured")

    try:
        vendor_response = await c.relay.generate_credit_appraisal(
            masked_data=masked_payload,
            masked_pdfs=masked_pdfs,
        )
    except VendorCallError as e:
        c.store.update_session_meta(
            reference_id,
            {"status": "vendor_call_failed", "error": str(e), "attempts": e.attempts},
        )
        c.audit.write(
            event="vendor_call_retry_failed",
            reference_id=reference_id,
            details={"error": str(e), "attempts": e.attempts, "status_code": e.status_code},
        )
        c.failed_queue.add(
            reference_id=reference_id,
            stage="vendor_call_manual_retry",
            reason=str(e),
            http_status=e.status_code,
            attempts=e.attempts,
            details={"is_timeout": e.is_timeout},
        )
        raise HTTPException(
            status_code=504 if e.is_timeout else 502,
            detail=f"Retry failed after {e.attempts} attempt(s): {e}",
        ) from e

    c.postgres.save_vendor_exchange(
        reference_id=reference_id, filename="vendor_response_raw.json", content=_serialize(vendor_response)
    )
    unmasked = c.masker.unmask_value(vendor_response, reference_id)
    c.store.update_session_meta(reference_id, {"status": "completed", "result": unmasked})
    c.audit.write(event="vendor_unmasked_manual_retry", reference_id=reference_id, details={})
    c.failed_queue.remove(reference_id)

    return {"reference_id": reference_id, "status": "completed", "result": unmasked}
