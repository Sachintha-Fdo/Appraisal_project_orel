"""
PII redaction pipeline for bank statement PDFs.

Ported from the standalone PDF_masking/app.py prototype into a reusable
service: OCR (PaddleOCR) + label/regex-based PII detection, then black-box
redaction of the matched regions via PyMuPDF.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

import fitz

from app.services.pdf_masking.detector import PIIDetector
from app.services.pdf_masking.ocr import OCRProcessor
from app.services.pdf_masking.pdf_processor import PDFProcessor
from app.services.pdf_masking.redactor import Redactor

DEFAULT_OCR_ZOOM = 4


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def _line_rect(line) -> fitz.Rect:
    if isinstance(line, fitz.Rect):
        return line

    if isinstance(line, (tuple, list)) and len(line) == 4 and all(isinstance(coord, (int, float)) for coord in line):
        return fitz.Rect(*line)

    if not isinstance(line, dict):
        raise TypeError(f"Unsupported rect input: {type(line)!r}")

    bbox = line.get("bbox")
    if isinstance(bbox, fitz.Rect):
        return bbox

    if isinstance(bbox, (tuple, list)) and len(bbox) == 4 and all(isinstance(coord, (int, float)) for coord in bbox):
        return fitz.Rect(*bbox)

    if isinstance(bbox, (tuple, list)) and all(isinstance(point, (tuple, list)) and len(point) == 2 for point in bbox):
        points = [tuple(point) for point in bbox]
        x0 = min(point[0] for point in points)
        y0 = min(point[1] for point in points)
        x1 = max(point[0] for point in points)
        y1 = max(point[1] for point in points)
        return fitz.Rect(x0, y0, x1, y1)

    raise TypeError(f"Unsupported bbox format: {type(bbox)!r}")


def _rect_union(lines):
    if not lines:
        return None
    rect = _line_rect(lines[0])
    for line in lines[1:]:
        rect |= _line_rect(line)
    return rect


def _find_block_rect(line_texts, ocr_lines, label_pattern, stop_pattern, max_lines=4):
    for idx, line_text in enumerate(line_texts):
        if re.search(label_pattern, line_text):
            end_idx = idx
            for next_idx in range(idx + 1, min(len(line_texts), idx + 1 + max_lines)):
                if not line_texts[next_idx].strip():
                    break
                if re.search(stop_pattern, line_texts[next_idx]):
                    break
                end_idx = next_idx
            return _rect_union(ocr_lines[idx:end_idx + 1])
    return None


def _expand_right_from_label(line_texts, ocr_lines, label_pattern):
    for idx, line_text in enumerate(line_texts):
        if re.search(label_pattern, line_text):
            label_rect = _line_rect(ocr_lines[idx])
            candidates = []
            for j, other_line in enumerate(ocr_lines):
                if j == idx:
                    continue
                other_rect = _line_rect(other_line)
                if other_rect.y0 < label_rect.y1 and other_rect.y1 > label_rect.y0:
                    if other_rect.x0 >= label_rect.x1 - 5:
                        candidates.append((other_rect.x0, j))
            if candidates:
                nearest = min(candidates, key=lambda item: item[0])
                return _rect_union([ocr_lines[idx], ocr_lines[nearest[1]]])
            return label_rect
    return None


def _find_account_label_digit_rect(line_texts, ocr_lines, label_pattern, digit_pattern):
    for idx, line_text in enumerate(line_texts):
        if re.search(label_pattern, line_text):
            label_rect = _line_rect(ocr_lines[idx])
            if re.search(digit_pattern, line_text):
                return label_rect

            candidates = []
            for j, other_line in enumerate(ocr_lines):
                if j == idx:
                    continue
                other_rect = _line_rect(other_line)
                other_text = _normalize_text(other_line["text"])
                if not re.search(digit_pattern, other_text):
                    continue
                if other_rect.x1 < label_rect.x0 - 10:
                    continue
                vertical_overlap = min(label_rect.y1, other_rect.y1) - max(label_rect.y0, other_rect.y0)
                vertical_gap = max(0.0, other_rect.y0 - label_rect.y1, label_rect.y0 - other_rect.y1)
                if vertical_overlap >= -30 or vertical_gap <= 40:
                    distance = max(0.0, other_rect.x0 - label_rect.x1)
                    candidates.append((distance + vertical_gap * 0.5, j))

            if candidates:
                nearest = min(candidates, key=lambda item: item[0])
                return _rect_union([ocr_lines[idx], ocr_lines[nearest[1]]])

            for next_idx in range(idx + 1, min(len(line_texts), idx + 5)):
                if re.search(digit_pattern, line_texts[next_idx]):
                    return _rect_union([ocr_lines[idx], ocr_lines[next_idx]])
    return None


def _find_title_name_rect(line_texts, ocr_lines, title_pattern):
    for idx, line_text in enumerate(line_texts):
        if re.match(title_pattern, line_text):
            return _line_rect(ocr_lines[idx])
    return None


def _find_label_value_rect(line_texts, ocr_lines, label_pattern, value_pattern, stop_pattern=None, max_lines=2):
    for idx, line_text in enumerate(line_texts):
        if re.search(label_pattern, line_text):
            label_rect = _line_rect(ocr_lines[idx])
            if re.search(value_pattern, line_text):
                return label_rect
            for next_idx in range(idx + 1, min(len(line_texts), idx + 1 + max_lines)):
                if not line_texts[next_idx].strip():
                    break
                if stop_pattern and re.search(stop_pattern, line_texts[next_idx]):
                    break
                if re.search(value_pattern, line_texts[next_idx]):
                    return _rect_union([ocr_lines[idx], ocr_lines[next_idx]])
            return label_rect
    return None


def _find_title_prefixed_rects(ocr_lines):
    rects = []
    title_prefix = re.compile(r"^(mr|mrs|ms|miss|dr)\b\.?", re.I)
    for idx, line in enumerate(ocr_lines):
        text = _normalize_text(line["text"])
        if title_prefix.match(text):
            rect = _line_rect(line)
            if re.fullmatch(r"^(mr|mrs|ms|miss|dr)\b\.?", text):
                if idx + 1 < len(ocr_lines):
                    next_text = _normalize_text(ocr_lines[idx + 1]["text"])
                    if re.search(r"[a-z]{2,}", next_text):
                        rect = _rect_union([rect, _line_rect(ocr_lines[idx + 1])])
            if rect not in rects:
                rects.append(rect)
    return rects


def _find_account_number_rects(ocr_lines):
    rects = []
    label_pattern = re.compile(
        r"\b(?:account(?:\s*(?:no|number|title|#))?|acct(?:\s*(?:no|number|title|#))?|a/c(?:\s*(?:no|number))?)\b",
        re.I,
    )
    digit_pattern = re.compile(r"[0-9*][0-9*\-\s\|\.]{4,}[0-9*]")
    for idx, line in enumerate(ocr_lines):
        line_text = _normalize_text(line["text"])
        if not re.search(label_pattern, line_text):
            continue
        label_rect = _line_rect(line)
        if re.search(digit_pattern, line_text):
            rects.append(label_rect)
            continue

        candidates = []
        for j, other_line in enumerate(ocr_lines):
            if j == idx:
                continue
            other_text = _normalize_text(other_line["text"])
            digits = re.sub(r"\D", "", other_text)
            if len(digits) < 4:
                continue
            other_rect = _line_rect(other_line)
            if other_rect.x1 < label_rect.x0 - 10:
                continue
            vertical_overlap = min(label_rect.y1, other_rect.y1) - max(label_rect.y0, other_rect.y0)
            vertical_gap = max(0.0, other_rect.y0 - label_rect.y1, label_rect.y0 - other_rect.y1)
            if vertical_overlap < -30 and vertical_gap > 60:
                continue
            if other_rect.x0 > label_rect.x1 + 250 and vertical_gap > 40:
                continue
            score = vertical_gap * 2 + max(0.0, other_rect.x0 - label_rect.x1)
            candidates.append((score, j, other_rect, digits))

        if candidates:
            candidates.sort(key=lambda item: item[0])
            best = candidates[0]
            rects.append(_rect_union([line, ocr_lines[best[1]]]))
            continue

        grouped = []
        for j in range(idx + 1, min(len(ocr_lines), idx + 6)):
            other_text = _normalize_text(ocr_lines[j]["text"])
            digits = re.sub(r"\D", "", other_text)
            if len(digits) >= 2 and re.search(r"[0-9\|\.\-]", other_text):
                grouped.append(j)
        if grouped:
            rects.append(_rect_union([line] + [ocr_lines[j] for j in grouped]))

    unique = []
    for rect in rects:
        if rect not in unique:
            unique.append(rect)
    return unique


def _ocr_search_rects(entity_text, entity_type, ocr_lines):
    rects = []
    search_text = _normalize_text(entity_text)
    line_texts = [_normalize_text(line["text"]) for line in ocr_lines]

    def add_rect(idx):
        rect = _line_rect(ocr_lines[idx])
        if rect not in rects:
            rects.append(rect)

    def add_rect_range(start, end):
        rect = _rect_union(ocr_lines[start:end + 1])
        if rect and rect not in rects:
            rects.append(rect)

    account_label_pattern = r"\b(?:account(?:\s*no|\s*number|\s*title)?|acct(?:\s*no|\s*number|\s*title)?|a/c(?:\s*no|\s*number)?)\b"
    name_label_pattern = r"\b(?:account(?:\s*name|\s*title)|acct(?:\s*name|\s*title)|customer\s*name|holder\s*name|name|title)\b"
    address_label_pattern = r"\b(?:address|addr|billing\s*address|residential\s*address|resident\s*address)\b"
    stop_pattern = r"\b(?:account|acct|a/c|name|title|date|statement|balance|phone|fax|branch|email|@)\b"

    if search_text:
        for idx, line_text in enumerate(line_texts):
            if search_text in line_text:
                add_rect(idx)
        if rects:
            return rects

        if len(search_text) > 10:
            for start in range(len(line_texts)):
                combined = ""
                for end in range(start, min(len(line_texts), start + 5)):
                    combined = (combined + " " + line_texts[end]).strip()
                    if search_text in combined:
                        add_rect_range(start, end)
                        return rects

    if entity_type == "ACCOUNT_NUMBER":
        target_digits = re.sub(r"\D", "", search_text)
        digit_label_rect = _find_account_label_digit_rect(
            line_texts,
            ocr_lines,
            account_label_pattern,
            r"[0-9*][0-9*\-\s]{4,}[0-9*]",
        )
        if digit_label_rect:
            rects.append(digit_label_rect)
            return rects

        labelled_rect = _find_label_value_rect(
            line_texts,
            ocr_lines,
            account_label_pattern,
            r"[0-9*][0-9*\-\s]{4,}[0-9*]",
            stop_pattern,
            max_lines=2,
        )
        if labelled_rect:
            rects.append(labelled_rect)
            return rects

    if entity_type == "ACCOUNT_NAME":
        title_name_rect = _find_title_name_rect(
            line_texts,
            ocr_lines,
            r"^(mr|ms|mrs|miss|dr)\b\.?(\s+[A-Za-z0-9 .,'\-/]+)?",
        )
        if title_name_rect:
            rects.append(title_name_rect)
            return rects

        target_digits = re.sub(r"\D", "", search_text)
        if target_digits:
            for idx, line_text in enumerate(line_texts):
                found_digits = re.sub(r"\D", "", line_text)
                if target_digits in found_digits:
                    add_rect(idx)
                    return rects
            for idx, line_text in enumerate(line_texts):
                if re.search(account_label_pattern, line_text):
                    if re.search(r"[0-9*][0-9*\-\s]{6,}[0-9*]", line_text):
                        add_rect(idx)
                        return rects
                    if idx + 1 < len(line_texts):
                        next_digits = re.sub(r"\D", "", line_texts[idx + 1])
                        if target_digits in next_digits or next_digits.endswith(target_digits[-4:]):
                            add_rect_range(idx, idx + 1)
                            return rects
                        right_rect = _expand_right_from_label(line_texts, ocr_lines, account_label_pattern)
                        if right_rect:
                            rects.append(right_rect)
                            return rects
            if len(target_digits) >= 4:
                end_digits = target_digits[-4:]
                for idx, line_text in enumerate(line_texts):
                    if end_digits in line_text and re.search(r"[0-9*]{4,}", line_text):
                        add_rect(idx)
                        return rects
        labelled_rect = _find_block_rect(line_texts, ocr_lines, account_label_pattern, stop_pattern, max_lines=2)
        if labelled_rect:
            rects.append(labelled_rect)
            return rects

    if entity_type == "ADDRESS":
        labelled_rect = _find_block_rect(line_texts, ocr_lines, address_label_pattern, stop_pattern, max_lines=4)
        if labelled_rect:
            rects.append(labelled_rect)
            return rects
        for idx, line_text in enumerate(line_texts):
            if re.search(r"\b(?:mr|mrs|ms|miss|dr)\b", line_text):
                address_lines = []
                for next_idx in range(idx + 1, min(len(line_texts), idx + 6)):
                    if not line_texts[next_idx].strip():
                        break
                    if re.search(stop_pattern, line_texts[next_idx]):
                        break
                    address_lines.append(next_idx)
                if address_lines:
                    add_rect_range(address_lines[0], address_lines[-1])
                    return rects

    if entity_type == "ACCOUNT_NAME":
        labelled_rect = _find_block_rect(line_texts, ocr_lines, name_label_pattern, stop_pattern, max_lines=3)
        if labelled_rect:
            rects.append(labelled_rect)
            return rects
        for idx, line_text in enumerate(line_texts):
            if re.search(r"\b(?:mr|mrs|ms|miss|dr)\b\.?\s+[a-z0-9 .,'\-/]{2,}", line_text):
                add_rect(idx)
                return rects

    if search_text:
        for idx, line_text in enumerate(line_texts):
            if all(word in line_text for word in search_text.split()[:3] if word):
                add_rect(idx)
    return rects


class PdfPiiMasker:
    """
    Detects and redacts PII (customer names, addresses, account numbers) from
    bank statement PDFs, combining native PDF text extraction with PaddleOCR
    for scanned/low-text pages.

    Loads the PaddleOCR model once per instance, so a single `PdfPiiMasker`
    should be reused across requests rather than re-created per PDF.
    """

    def __init__(self, ocr_zoom: int = DEFAULT_OCR_ZOOM):
        self._detector = PIIDetector()
        self._ocr = OCRProcessor()
        self._zoom = ocr_zoom

    def _ocr_text_and_rects(self, page, pdf: PDFProcessor):
        image_bytes = pdf.page_to_image_bytes(page, zoom=self._zoom)
        ocr_lines = self._ocr.extract_text(image_bytes)
        ocr_text = "\n".join(line["text"] for line in ocr_lines)
        if self._zoom != 1:
            scale = 1.0 / self._zoom
            for line in ocr_lines:
                bbox = line.get("bbox", [])
                if not bbox:
                    continue
                if len(bbox) == 4 and all(isinstance(coord, (int, float)) for coord in bbox):
                    line["bbox"] = [coord * scale for coord in bbox]
                elif all(isinstance(point, (list, tuple)) and len(point) == 2 for point in bbox):
                    line["bbox"] = [[point[0] * scale, point[1] * scale] for point in bbox]
                elif len(bbox) % 2 == 0:
                    scaled = [coord * scale for coord in bbox]
                    line["bbox"] = [scaled[i:i + 2] for i in range(0, len(scaled), 2)]
                else:
                    line["bbox"] = bbox
        return ocr_text, ocr_lines

    def process_pdf(self, pdf_path: str, output_path: Optional[str] = None) -> str:
        """Redact PII from `pdf_path` and write the result to `output_path`."""
        if output_path is None:
            output_path = str(Path("masked_pdfs") / f"masked_{Path(pdf_path).stem}.pdf")

        Path(output_path).parent.mkdir(parents=True, exist_ok=True)

        pdf = PDFProcessor(pdf_path)
        try:
            for page in pdf.pages():
                page_text = pdf.extract_text(page)
                page_text_stripped = page_text.strip()
                rects_to_redact = []
                entities = self._detector.detect(page_text_stripped)
                use_ocr = len(page_text_stripped) < 20 or not page.get_text("words")
                ocr_lines = None

                if use_ocr:
                    page_text_stripped, ocr_lines = self._ocr_text_and_rects(page, pdf)

                    text_entities = self._detector.detect(page_text_stripped)
                    line_entities = self._detector.detect_from_lines(ocr_lines)
                    entities = self._detector._dedupe_entities(text_entities + line_entities)

                    for entity in entities:
                        rects = _ocr_search_rects(entity.get("text"), entity.get("type"), ocr_lines)
                        if rects:
                            rects_to_redact.extend(rects)
                else:
                    for entity in entities:
                        rects = pdf.search(page, entity.get("text"))
                        if not rects:
                            if ocr_lines is None:
                                _, ocr_lines = self._ocr_text_and_rects(page, pdf)
                            rects = _ocr_search_rects(entity.get("text"), entity.get("type"), ocr_lines)
                        if rects:
                            rects_to_redact.extend(rects)

                if ocr_lines is not None:
                    title_rects = _find_title_prefixed_rects(ocr_lines)
                    for rect in title_rects:
                        if rect not in rects_to_redact:
                            rects_to_redact.append(rect)

                    account_rects = _find_account_number_rects(ocr_lines)
                    for rect in account_rects:
                        if rect not in rects_to_redact:
                            rects_to_redact.append(rect)

                    fallback_rects = _ocr_search_rects("", "ACCOUNT_NUMBER", ocr_lines)
                    for rect in fallback_rects:
                        if rect not in rects_to_redact:
                            rects_to_redact.append(rect)

                if rects_to_redact:
                    Redactor.redact(page, rects_to_redact)

            pdf.save(output_path)
        finally:
            pdf.close()

        return output_path
