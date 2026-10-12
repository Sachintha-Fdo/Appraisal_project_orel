#!/usr/bin/env python3
"""
One-off test: submits examples/new_request_payload.json against the running
local Orel Server API, which forwards the masked payload to CDB Local Server
(CDB_LOCAL_SERVER_URL), which relays it to Azure (AZURE_INBOUND_URL). Requires
both this app (`uvicorn app.main:app --port 8000`, from this folder) and CDB
Local Server (`uvicorn app.cdb_local_server.main:app --port 8100`, from the
sibling `../cdb-local-server` folder) running. Saves the full response under
system_test_results/.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

PROJ = Path(__file__).resolve().parent
BASE = os.environ.get("API_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
PAYLOAD_PATH = PROJ / "examples" / "new_request_payload.json"

# file_banks value -> local sample PDF to upload under that filename.
# "auto" (statement_3.pdf) has no bank-specific sample, so use a generic one.
BANK_PDF_MAP: dict[str, Path] = {
    "hnb": PROJ / "HNB.pdf",
    "boc": PROJ / "BOC.pdf",
    "ntb": PROJ / "NTB.pdf",
    "sampath": PROJ / "Sampath.pdf",
    "auto": PROJ / "NTB.pdf",
}


def _load_proj_dotenv() -> None:
    # .env then endpoints.env — matches app/config.py's own load order
    # (CDB_LOCAL_SERVER_URL, AZURE_INBOUND_URL, etc. live in the latter).
    for filename in (".env", "endpoints.env"):
        p = PROJ / filename
        if not p.is_file():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val


def _resolve_pdfs(file_banks: dict[str, str]) -> list[tuple[str, Path]]:
    resolved: list[tuple[str, Path]] = []
    for upload_name, bank in file_banks.items():
        local_pdf = BANK_PDF_MAP.get(str(bank).strip().lower())
        if local_pdf and local_pdf.is_file():
            resolved.append((upload_name, local_pdf))
        else:
            print(f"Warning: no sample PDF for bank '{bank}' ({upload_name}); skipping.", file=sys.stderr)
    return resolved


def _save_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _wait_health(client: httpx.Client, max_wait_s: int = 45) -> httpx.Response:
    deadline = time.time() + max_wait_s
    last_err: str | None = None
    while time.time() < deadline:
        try:
            r = client.get(f"{BASE}/api/v1/health", timeout=5.0)
            if r.status_code == 200:
                return r
            last_err = f"HTTP {r.status_code}"
        except Exception as e:
            last_err = str(e)
        time.sleep(1)
    raise RuntimeError(f"API not reachable at {BASE}: {last_err}")


def main() -> int:
    _load_proj_dotenv()
    if not PAYLOAD_PATH.is_file():
        print(f"Missing payload file: {PAYLOAD_PATH}", file=sys.stderr)
        return 1

    run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_utc")
    out = PROJ / "system_test_results" / f"new_payload_run_{run_id}"
    out.mkdir(parents=True, exist_ok=True)

    payload_obj = json.loads(PAYLOAD_PATH.read_text(encoding="utf-8"))
    pdfs = _resolve_pdfs(payload_obj.get("file_banks") or {})

    summary: dict = {
        "run_id": run_id,
        "base_url": BASE,
        "cdb_local_server_url_from_env": os.environ.get("CDB_LOCAL_SERVER_URL"),
        "azure_inbound_url_from_env": os.environ.get("AZURE_INBOUND_URL"),
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "payload_file": str(PAYLOAD_PATH),
        "pdf_files": {name: str(path) for name, path in pdfs},
    }

    timeout = httpx.Timeout(900.0, connect=30.0)
    with httpx.Client(timeout=timeout) as client:
        hresp = _wait_health(client)
        summary["health_status"] = hresp.status_code

        _save_json(out / "request_payload.json", payload_obj)

        data = {"data": json.dumps(payload_obj, ensure_ascii=False)}
        open_files = [open(p, "rb") for _, p in pdfs]
        try:
            files = [("file", (name, fh, "application/pdf")) for (name, _), fh in zip(pdfs, open_files)]
            resp = client.post(f"{BASE}/public/generate-credit-appraisal", data=data, files=files or None)
        finally:
            for fh in open_files:
                fh.close()

        body = resp.json() if resp.content else {"raw": resp.text}
        _save_json(
            out / "response.json",
            {
                "endpoint": "POST /public/generate-credit-appraisal",
                "status_code": resp.status_code,
                "response_body": body,
            },
        )
        summary["status_code"] = resp.status_code
        summary["reference_id"] = body.get("reference_id") if isinstance(body, dict) else None

        if isinstance(body, dict) and body.get("result") is not None:
            _save_json(out / "unmasked_result.json", body["result"])
        if isinstance(body, dict) and body.get("masked_payload") is not None:
            _save_json(out / "masked_payload.json", body["masked_payload"])

        # --- Status + masked PDFs: masked PDFs now live in Postgres (masked_pdfs
        # table), not on disk, so fetch them from there as local evidence files.
        reference_id = summary["reference_id"]
        masked_pdfs_dir = out / "masked_pdfs"
        if reference_id:
            st = client.get(f"{BASE}/api/v1/appraisal/{reference_id}/status")
            summary["status_endpoint_code"] = st.status_code

            database_url = os.environ.get("DATABASE_URL")
            if database_url:
                try:
                    import psycopg

                    with psycopg.connect(database_url, connect_timeout=5) as conn:
                        rows = conn.execute(
                            "SELECT filename, content FROM masked_pdfs WHERE reference_id = %s ORDER BY id",
                            (reference_id,),
                        ).fetchall()
                    masked_pdfs_dir.mkdir(exist_ok=True)
                    saved = []
                    for filename, content in rows:
                        dest = masked_pdfs_dir / filename
                        dest.write_bytes(bytes(content))
                        saved.append(str(dest))
                    summary["masked_pdfs_saved"] = saved
                except Exception as e:
                    print(f"Warning: could not fetch masked PDFs from Postgres: {e}", file=sys.stderr)

    summary["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
    summary["output_directory"] = str(out)
    _save_json(out / "TEST_SUMMARY.json", summary)

    print(json.dumps(summary, indent=2))
    print(f"\nArtifacts: {out}")
    return 0 if summary["status_code"] == 200 else 1


if __name__ == "__main__":
    raise SystemExit(main())
