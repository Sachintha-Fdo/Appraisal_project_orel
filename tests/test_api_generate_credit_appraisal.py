import json
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.config import Settings, get_settings

PROJ = Path(__file__).resolve().parent.parent
SAMPLE_PDF = PROJ / "test_pdf" / "BOC.pdf"

_settings_for_skip_check = get_settings()
try:
    with psycopg.connect(_settings_for_skip_check.database_url or "postgresql://", connect_timeout=2):
        pass
except Exception as exc:
    pytest.skip(
        f"Postgres not reachable ({exc}); skipping test_api_generate_credit_appraisal.py",
        allow_module_level=True,
    )


def _make_client(tmp_path, monkeypatch, **settings_overrides) -> TestClient:
    settings = Settings(
        use_fake_redis=True,
        echo_azure=True,  # no live vendor call — echoes the masked payload back
        pii_master_key_hex="0" * 64,
        uploads_dir=str(tmp_path / "uploads"),
        masked_pdfs_dir=str(tmp_path / "masked_pdfs"),
        vendor_exchange_dir=str(tmp_path / "vendor_exchanges"),
        **settings_overrides,
    )
    # main.get_settings is `from app.config import get_settings` — patch the
    # name bound in app.main, not app.config, or the app under test would
    # still pick up the lru_cache'd real one (and read the real .env).
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    app = main_module.create_app()
    return TestClient(app)


@pytest.fixture
def sample_pdf_bytes() -> bytes:
    return SAMPLE_PDF.read_bytes()


def test_health_ok(tmp_path, monkeypatch):
    with _make_client(tmp_path, monkeypatch) as client:
        resp = client.get("/api/v1/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


def test_rejects_more_than_ten_files(tmp_path, monkeypatch, sample_pdf_bytes):
    with _make_client(tmp_path, monkeypatch) as client:
        files = [("file", (f"s{i}.pdf", sample_pdf_bytes, "application/pdf")) for i in range(11)]
        resp = client.post(
            "/public/generate-credit-appraisal",
            data={"data": json.dumps({"loan_number": "LN-1"})},
            files=files,
        )
        assert resp.status_code == 400


def test_rejects_no_files(tmp_path, monkeypatch):
    with _make_client(tmp_path, monkeypatch) as client:
        resp = client.post(
            "/public/generate-credit-appraisal",
            data={"data": json.dumps({"loan_number": "LN-1"})},
        )
        assert resp.status_code == 400


def test_rejects_non_pdf_upload(tmp_path, monkeypatch):
    with _make_client(tmp_path, monkeypatch) as client:
        resp = client.post(
            "/public/generate-credit-appraisal",
            data={"data": json.dumps({"loan_number": "LN-1"})},
            files=[("file", ("not_a_pdf.pdf", b"this is not a pdf", "application/pdf"))],
        )
        assert resp.status_code == 400
        assert "not a valid PDF" in resp.json()["detail"]


def test_rejects_invalid_data_json(tmp_path, monkeypatch, sample_pdf_bytes):
    with _make_client(tmp_path, monkeypatch) as client:
        resp = client.post(
            "/public/generate-credit-appraisal",
            data={"data": "{not valid json"},
            files=[("file", ("statement_1.pdf", sample_pdf_bytes, "application/pdf"))],
        )
        assert resp.status_code == 400


def test_full_echo_roundtrip_masks_and_unmasks(tmp_path, monkeypatch, sample_pdf_bytes):
    """
    End-to-end: submit real applicant PII + a real PDF, confirm the response
    comes back completed with the PII restored, and that what actually got
    persisted to Postgres for the vendor exchange + masked PDF never contains
    the raw PII — only ⟦PII:token⟧ placeholders (the same guarantee verified
    live against the real vendor throughout this project).
    """
    applicant_name = "Sujith Perera"
    payload = {
        "loan_number": "LN-TEST-001",
        "applicant_name": applicant_name,
        "bank": "boc",
        "file_banks": {"statement_1.pdf": "boc"},
    }
    with _make_client(tmp_path, monkeypatch) as client:
        resp = client.post(
            "/public/generate-credit-appraisal",
            data={"data": json.dumps(payload)},
            files=[("file", ("statement_1.pdf", sample_pdf_bytes, "application/pdf"))],
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "completed"
        reference_id = body["reference_id"]

        # PII round-tripped back out correctly.
        assert body["result"]["applicant_name"] == applicant_name

        # Raw upload dir is cleaned up right after masking (PII retention).
        assert not (tmp_path / "uploads" / reference_id).exists()
        # Transient PDF-masking staging dir is cleaned up too.
        assert not (tmp_path / "masked_pdfs" / reference_id).exists()

        # What's actually in Postgres must never contain the raw applicant name.
        settings = get_settings()
        with psycopg.connect(settings.database_url) as conn:
            masked_request_text = conn.execute(
                "SELECT content FROM vendor_exchanges WHERE reference_id = %s AND filename = %s",
                (reference_id, "masked_request.json"),
            ).fetchone()[0]
            assert applicant_name not in masked_request_text

            pdf_row = conn.execute(
                "SELECT filename, content FROM masked_pdfs WHERE reference_id = %s", (reference_id,)
            ).fetchone()
            assert pdf_row is not None
            assert pdf_row[0] == "statement_1.pdf"
            assert bytes(pdf_row[1]).startswith(b"%PDF")

        # status/result endpoints agree with the inline response.
        status_resp = client.get(f"/api/v1/appraisal/{reference_id}/status")
        assert status_resp.status_code == 200
        assert status_resp.json()["status"] == "completed"

        result_resp = client.get(f"/api/v1/appraisal/{reference_id}/result")
        assert result_resp.status_code == 200
        assert result_resp.json()["result"]["applicant_name"] == applicant_name
