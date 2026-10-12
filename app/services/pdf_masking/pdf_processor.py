import fitz
import re


class PDFProcessor:
    def __init__(self, pdf_path):
        self.pdf_path = pdf_path
        self.doc = fitz.open(pdf_path)

    def pages(self):
        """Return an iterable of page objects."""
        return self.doc

    def extract_text(self, page):
        """Extract text from a single page object."""
        return page.get_text()

    def page_to_image_bytes(self, page, zoom=4):
        """Render a page to high-resolution PNG bytes for OCR."""
        matrix = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=matrix, alpha=False)
        return pix.tobytes("png")

    def search(self, page, text):
        """Search for text on a page and return a list of rects (may be empty)."""
        if not text:
            return []

        text = text.strip()
        if not text:
            return []

        try:
            rects = page.search_for(text)
        except Exception:
            rects = []

        if rects:
            return rects

        words = page.get_text("words")
        if not words:
            return []

        normalized_text = re.sub(r"\s+", " ", text).strip().lower()
        target_words = normalized_text.split(" ")
        if not target_words:
            return []

        page_words = [w[4].strip().lower() for w in words]
        word_rects = [fitz.Rect(w[0], w[1], w[2], w[3]) for w in words]

        for i in range(len(page_words) - len(target_words) + 1):
            if page_words[i : i + len(target_words)] == target_words:
                rect = word_rects[i]
                rect |= word_rects[i + len(target_words) - 1]
                return [rect]

        return []

    def extract_images(self):
        images = []
        for page in self.doc:
            for img in page.get_images(full=True):
                xref = img[0]
                base_image = self.doc.extract_image(xref)
                image_bytes = base_image["image"]
                images.append(image_bytes)
        return images

    def save(self, output_path):
        self.doc.save(output_path)

    def close(self):
        self.doc.close()
