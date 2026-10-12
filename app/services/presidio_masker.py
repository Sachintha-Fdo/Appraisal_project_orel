from __future__ import annotations

import re
import secrets
import uuid
from typing import Any, Optional

from presidio_analyzer import AnalyzerEngine
from presidio_analyzer.nlp_engine import NlpEngineProvider

from app.config import Settings
from app.services.crypto_engine import AesGcmPiiVault
from app.services.redis_store import SessionStore

# PLACEHOLDER_RE = re.compile(r"⟦PII:([^⟧]+)⟧")
PLACEHOLDER_RE = re.compile(r"\[PII:([^]]+)\]")

# Presidio entity types we treat as PII (span-level masking in free text).
SPAN_PII_ENTITIES = frozenset(
    {
        "PERSON",
        "EMAIL_ADDRESS",
        "PHONE_NUMBER",
        "NRP",  # national ID / passport-style numbers when detected
        "IBAN_CODE",
        "CREDIT_CARD",
        "US_SSN",
        "US_PASSPORT",
        "US_DRIVER_LICENSE",
        "UK_NHS",
        "CRYPTO",
    }
)

# Fields whose entire string value is sensitive (exact leaf key match).
FULL_MASK_FIELD_KEYS = frozenset(
    {
        # Applicant identity / contact
        "applicant_name",
        "address",
        "spouse_name",
        "spouse_details",
        "contact_details",
        "family_details",
        # "education",
        # "business",
        # Staff / third-party names involved in the process
        "hob",
        "marketing_officer",
        "residence_inspection_by",
        "asset_inspection_by",
        "valuer",
        "supplier_name",
        "supplier_contact",
        # Guarantor identity / contact / employment
        "guarantor_name",
        "guarantor_address",
        "guarantor_employer",
        "guarantor_employment",
        "guarantor_2_name",
        "guarantor_2_address",
    }
)

MIN_PRESIDIO_SCORE = 0.45


class JsonPiiMasker:
   

    def __init__(
        self,
        settings: Settings,
        vault: AesGcmPiiVault,
        store: SessionStore,
    ):
        self._lang = settings.presidio_language
        self._vault = vault
        self._store = store
        extra = {k.strip().lower() for k in (settings.json_mask_keys or []) if k.strip()}
        self._full_mask_fields = FULL_MASK_FIELD_KEYS | extra

        nlp_configuration = {
            "nlp_engine_name": "spacy",
            "models": [
                {
                    "lang_code": self._lang,
                    "model_name": settings.presidio_spacy_model,
                }
            ],
        }
        nlp_engine = NlpEngineProvider(nlp_configuration=nlp_configuration).create_engine()
        self._analyzer = AnalyzerEngine(
            nlp_engine=nlp_engine,
            supported_languages=[self._lang],
        )

    def _pick_non_overlapping(self, results: list) -> list:
        spans = sorted(results, key=lambda r: (r.end - r.start), reverse=True)
        picked: list = []

        def overlaps(a, b) -> bool:
            return not (a.end <= b.start or b.end <= a.start)

        for r in spans:
            if not any(overlaps(r, p) for p in picked):
                picked.append(r)
        return sorted(picked, key=lambda r: r.start, reverse=True)

    def _should_mask_entire_field(self, key_path: list[str] | None) -> bool:
        if not key_path:
            return False
        leaf = key_path[-1]
        if not isinstance(leaf, str):
            return False
        leaf_lower = leaf.lower()
        return leaf_lower in self._full_mask_fields

    def _encrypt_placeholder(self, plaintext: str, reference_id: str) -> str:
        token_id = secrets.token_hex(8)
        blob = self._vault.encrypt(plaintext, reference_id)
        self._store.save_pii_blob(reference_id, token_id, blob)
        return f"[PII:{token_id}]"

    def _mask_entire_string(self, text: str, reference_id: str) -> str:
        if not text or not text.strip():
            return text
        return self._encrypt_placeholder(text, reference_id)

    def _is_example_or_placeholder(self, text: str) -> bool:
        lower = text.lower()
        if any(d in lower for d in ("@example.", "@test.", "@sample.", "@demo.")):
            return True
        if lower in ("n/a", "none", "unknown", "not provided"):
            return True
        return False

    def _should_mask_span(self, entity_type: str, span_text: str, score: float) -> bool:
        if score < MIN_PRESIDIO_SCORE:
            return False
        if entity_type not in SPAN_PII_ENTITIES:
            return False
        span = span_text.strip()
        if not span:
            return False
        if self._is_example_or_placeholder(span):
            return False
        if entity_type == "PERSON":
            # Avoid masking single generic tokens Presidio mislabels as persons.
            parts = span.split()
            if len(parts) == 1 and len(span) < 4:
                return False
            if len(parts) == 1 and span.lower() in {
                "married",
                "single",
                "director",
                "registered",
                "hybrid",
                "auto",
                "manual",
                "low",
                "medium",
                "high",
                "clean",
                "none",
            }:
                return False
        if entity_type == "PHONE_NUMBER":
            digits = re.sub(r"\D", "", span)
            if len(digits) < 9:
                return False
        return True

    def _mask_string_spans(self, text: str, reference_id: str) -> str:
        if not text or not text.strip():
            return text

        results = self._analyzer.analyze(
            text=text,
            language=self._lang,
            entities=list(SPAN_PII_ENTITIES),
            score_threshold=MIN_PRESIDIO_SCORE,
        )
        if not results:
            return text

        filtered = [
            r
            for r in results
            if self._should_mask_span(r.entity_type, text[r.start : r.end], r.score)
        ]
        if not filtered:
            return text

        for r in self._pick_non_overlapping(filtered):
            raw = text[r.start : r.end]
            if not raw.strip():
                continue
            placeholder = self._encrypt_placeholder(raw, reference_id)
            text = text[: r.start] + placeholder + text[r.end :]

        return text

    def mask_value(
        self,
        value: Any,
        reference_id: str,
        key_path: Optional[list[str]] = None,
    ) -> Any:
        if isinstance(value, str):
            if self._should_mask_entire_field(key_path):
                return self._mask_entire_string(value, reference_id)
            return self._mask_string_spans(value, reference_id)
        if isinstance(value, dict):
            return {
                k: self.mask_value(v, reference_id, (key_path or []) + [k])
                for k, v in value.items()
            }
        if isinstance(value, list):
            return [
                self.mask_value(v, reference_id, (key_path or []) + [str(i)])
                for i, v in enumerate(value)
            ]
        return value

    def unmask_string(self, text: str, reference_id: str) -> str:
        if not text or "[PII:" not in text:
            return text

        def repl(m: re.Match) -> str:
            token_id = m.group(1)
            blob = self._store.get_pii_blob(reference_id, token_id)
            if not blob:
                return m.group(0)
            return self._vault.decrypt(blob, reference_id)

        return PLACEHOLDER_RE.sub(repl, text)

    def unmask_value(self, value: Any, reference_id: str) -> Any:
        if isinstance(value, str):
            return self.unmask_string(value, reference_id)
        if isinstance(value, dict):
            return {k: self.unmask_value(v, reference_id) for k, v in value.items()}
        if isinstance(value, list):
            return [self.unmask_value(v, reference_id) for v in value]
        return value


def new_reference_id() -> str:
    return str(uuid.uuid4())
