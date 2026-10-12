import re


class PIIDetector:
    def __init__(self):
        self.account_number_pattern = re.compile(
            r"\b(?:ACCOUNT(?: NUMBER| NO| TITLE| #)?|ACCT(?: NUMBER| NO| TITLE| #)?|A/C(?: NUMBER| NO)?)[:\s\-]*([0-9*][0-9*\-\s]{6,}[0-9*])\b",
            re.I,
        )
        self.account_number_split_pattern = re.compile(
            r"\b(?:ACCOUNT(?: NUMBER| NO| TITLE| #)?|ACCT(?: NUMBER| NO| TITLE| #)?|A/C(?: NUMBER| NO)?)[:\s\-]*[\n\r]+\s*([0-9*][0-9*\-\s]{6,}[0-9*])\b",
            re.I,
        )
        self.account_name_label_pattern = re.compile(
            r"\b(?:ACCOUNT(?: NAME| TITLE)|ACCT(?: NAME| TITLE)|CUSTOMER NAME|HOLDER NAME|NAME|TITLE)[:\s\-]*([A-Z][A-Za-z .,'\-]{2,})",
            re.I,
        )
        self.account_name_split_pattern = re.compile(
            r"\b(?:ACCOUNT(?: NAME| TITLE)|ACCT(?: NAME| TITLE)|CUSTOMER NAME|HOLDER NAME|NAME|TITLE)[:\s\-]*[\n\r]+\s*([A-Z][A-Za-z .,'\-]{2,})\b",
            re.I,
        )
        self.title_name_pattern = re.compile(
            r"\b(?:MR|MRS|MS|MISS|DR)\b\.?\s+[A-Z][A-Z0-9 .,'\-/]{2,}",
            re.I,
        )
        self.address_label_pattern = re.compile(
            r"\b(?:ADDRESS|ADDR|RESIDENTIAL ADDRESS|BILLING ADDRESS)[\s:\-]*([^\n\r]+(?:[\n\r]+\s*[^\n\r]+)?)",
            re.I,
        )
        self.street_pattern = re.compile(
            r"\b(?:\d{1,5}\s+)?[A-Za-z0-9 .,'#/-]+(?:STREET|ST|ROAD|RD|AVENUE|AVE|BOULEVARD|BLVD|LANE|LN|DRIVE|DR|COURT|CT|PLACE|PL|WAY|PARKWAY|PKWY|CIRCLE|CIR|TERRACE|TER|TRAIL|TRL|HIGHWAY|HWY|SUITE|STE|APARTMENT|APT|UNIT|#)\b",
            re.I,
        )
        self.city_state_zip_pattern = re.compile(
            r"\b[A-Za-z][A-Za-z .'-]+,\s*[A-Z]{2}\s+\d{5}(?:-\d{4})?\b",
            re.I,
        )
        self.address_continue_pattern = re.compile(
            r"\b(?:APT|APARTMENT|SUITE|STE|UNIT|FL|FLOOR|#\d+|\d{1,4})\b",
            re.I,
        )
        self.name_label_pattern = re.compile(
            r"\b(?:ACCOUNT(?: NAME| TITLE)|ACCT(?: NAME| TITLE)|CUSTOMER NAME|HOLDER NAME|NAME|TITLE)\b",
            re.I,
        )
        self.stop_pattern = re.compile(
            r"\b(?:ACCOUNT|ACCT|A/C|NAME|TITLE|DATE|STATEMENT|BALANCE|PHONE|FAX|BRANCH|EMAIL|@)\b",
            re.I,
        )
        self.date_pattern = re.compile(
            r"\b(?:DATE|\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4}|\d{4}[\/\-]\d{1,2}[\/\-]\d{1,2})\b",
            re.I,
        )
        self.generic_account_pattern = re.compile(r"\b[0-9*][0-9*\-\s]{6,}[0-9*]\b")

    def _as_line_text(self, line):
        if isinstance(line, dict):
            return str(line.get("text", "")).strip()
        return str(line).strip()

    def _line_address_score(self, line):
        stripped = self._as_line_text(line)
        if not stripped:
            return 0.0

        score = 0.0
        if self.street_pattern.search(stripped):
            score += 4.0
        if self.city_state_zip_pattern.search(stripped):
            score += 5.0
        if self.address_continue_pattern.search(stripped):
            score += 2.0

        if re.search(r"\b(?:STREET|ST|ROAD|RD|AVENUE|AVE|BOULEVARD|BLVD|LANE|LN|DRIVE|DR|COURT|CT|PLACE|PL|WAY|PARKWAY|PKWY|CIRCLE|CIR|TERRACE|TER|TRAIL|TRL|HIGHWAY|HWY)\b", stripped, re.I):
            score += 2.0

        if re.search(r"\d", stripped):
            score += 1.0

        if len(stripped.split()) < 2:
            score -= 3.0

        return score

    def _looks_like_address_line(self, line):
        return self._line_address_score(line) >= 3.0

    def _collect_unlabeled_address_block(self, lines):
        """Rank nearby blocks that resemble a mailing address, with proximity to the customer name preferred."""
        line_texts = [self._as_line_text(line) for line in lines]
        candidates = []
        name_positions = []
        for idx, line in enumerate(line_texts):
            if self.name_label_pattern.search(line) or self.title_name_pattern.search(line):
                name_positions.append(idx)

        for idx, line in enumerate(line_texts):
            stripped = line.strip()
            if not stripped:
                continue

            start_score = self._line_address_score(line)
            if start_score < 4.0:
                continue

            block_lines = [stripped]
            block_score = start_score
            name_distance = min(abs(idx - pos) for pos in name_positions) if name_positions else 99

            for next_idx in range(idx + 1, min(len(line_texts), idx + 4)):
                next_line = line_texts[next_idx].strip()
                if not next_line:
                    break
                if self.stop_pattern.search(next_line):
                    break

                next_score = self._line_address_score(line_texts[next_idx])
                if next_score < 2.0:
                    break

                block_lines.append(next_line)
                block_score += next_score

            candidate_text = "\n".join(block_lines)
            if len(block_lines) == 1 and not self.city_state_zip_pattern.search(candidate_text):
                continue
            if len(candidate_text.strip().split()) < 3:
                continue
            if self.date_pattern.search(candidate_text):
                continue
            if not self.city_state_zip_pattern.search(candidate_text) and not self.street_pattern.search(candidate_text):
                continue

            score = block_score - (name_distance * 0.35)
            candidates.append((score, candidate_text, idx))

        if not candidates:
            return None

        candidates.sort(key=lambda item: item[0], reverse=True)
        best_score, best_text, _ = candidates[0]
        if best_score < 6.0:
            return None
        return best_text

    def _dedupe_entities(self, entities):
        seen = set()
        unique = []
        for entity in entities:
            key = (entity.get("text"), entity.get("type"))
            if key not in seen and entity.get("text"):
                seen.add(key)
                unique.append(entity)
        return unique

    def detect_from_lines(self, ocr_lines):
        entities = []
        candidate_text = self._collect_unlabeled_address_block(ocr_lines)
        if candidate_text and not self.date_pattern.search(candidate_text):
            entities.append({"text": candidate_text, "type": "ADDRESS"})

        for idx, line in enumerate(ocr_lines):
            text = line.get("text", "")
            if not text:
                continue
            for match in self.account_name_label_pattern.finditer(text):
                name = match.group(1).strip()
                if not self.date_pattern.search(name):
                    entities.append({"text": name, "type": "ACCOUNT_NAME"})
            for match in self.title_name_pattern.finditer(text):
                name = match.group().strip()
                if not self.date_pattern.search(name):
                    entities.append({"text": name, "type": "ACCOUNT_NAME"})
            for match in self.account_number_pattern.finditer(text):
                entities.append({"text": match.group(1).strip(), "type": "ACCOUNT_NUMBER"})

        return self._dedupe_entities(entities)

    def detect(self, text):
        """Return a list of detected PII entities as dicts: {"text":..., "type":...}"""
        entities = []

        for match in self.account_number_pattern.finditer(text):
            entities.append({"text": match.group(1).strip(), "type": "ACCOUNT_NUMBER"})
        for match in self.account_number_split_pattern.finditer(text):
            entities.append({"text": match.group(1).strip(), "type": "ACCOUNT_NUMBER"})

        for match in self.account_name_label_pattern.finditer(text):
            name = match.group(1).strip()
            if not self.date_pattern.search(name):
                entities.append({"text": name, "type": "ACCOUNT_NAME"})
        for match in self.account_name_split_pattern.finditer(text):
            name = match.group(1).strip()
            if not self.date_pattern.search(name):
                entities.append({"text": name, "type": "ACCOUNT_NAME"})

        for match in self.title_name_pattern.finditer(text):
            name = match.group().strip()
            if not self.date_pattern.search(name):
                entities.append({"text": name, "type": "ACCOUNT_NAME"})

        for match in self.address_label_pattern.finditer(text):
            address_value = match.group(1).strip()
            if not self.date_pattern.search(address_value):
                entities.append({"text": address_value, "type": "ADDRESS"})

        lines = [line.strip() for line in text.splitlines()]
        unlabeled_address = self._collect_unlabeled_address_block(lines)
        if unlabeled_address and not self.date_pattern.search(unlabeled_address):
            entities.append({"text": unlabeled_address, "type": "ADDRESS"})

        for match in self.generic_account_pattern.finditer(text):
            number = match.group().strip()
            if not self.date_pattern.search(number):
                entities.append({"text": number, "type": "ACCOUNT_NUMBER"})

        return self._dedupe_entities(entities)
