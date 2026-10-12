import fitz


class Redactor:
    @staticmethod
    def redact(page, rects, fill=(0, 0, 0)):
        """Apply redaction annotations to a page for the given rects and apply them."""
        if not rects:
            return
        for r in rects:
            page.add_redact_annot(r, fill=fill)
        page.apply_redactions()

    @staticmethod
    def redact_text(pdf_path, output_path, text_to_redact):
        doc = fitz.open(pdf_path)
        for page in doc:
            text_instances = page.search_for(text_to_redact)
            for inst in text_instances:
                page.add_redact_annot(inst, fill=(0, 0, 0))
            page.apply_redactions()
        doc.save(output_path)
        doc.close()
