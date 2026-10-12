import os
import shutil
from pathlib import Path
from typing import List, Tuple

from app.config import Settings
from app.services.pdf_masking.masker import PdfPiiMasker
from app.services.postgres_store import PostgresAuditStore


class BankStatementPdfService:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._masker = PdfPiiMasker(ocr_zoom=settings.pdf_ocr_zoom)

    def mask_pdfs(
        self, input_paths: List[str], reference_id: str, store: PostgresAuditStore
    ) -> List[Tuple[str, bytes]]:
        """
        Redact each PDF, persist the masked bytes to Postgres under its
        original filename (not a renamed/prefixed one) so the vendor's
        `file_banks` filename->bank mapping still resolves correctly once
        forwarded, and return (filename, content) pairs for the caller.

        masked_pdfs_dir is only a transient staging area for PdfPiiMasker's
        own file-based processing — cleaned up in `finally` regardless of
        outcome, same PII-minimization posture as the raw-upload cleanup in
        app/api/routes_appraisal.py.
        """
        out_dir = os.path.join(self._settings.masked_pdfs_dir, reference_id)
        os.makedirs(out_dir, exist_ok=True)

        try:
            outputs: List[Tuple[str, bytes]] = []
            for p in input_paths:
                base = Path(p).name
                out_path = os.path.join(out_dir, base)
                self._masker.process_pdf(p, output_path=out_path)
                with open(out_path, "rb") as f:
                    content = f.read()
                store.save_masked_pdf(reference_id=reference_id, filename=base, content=content)
                outputs.append((base, content))
            return outputs
        finally:
            shutil.rmtree(out_dir, ignore_errors=True)
