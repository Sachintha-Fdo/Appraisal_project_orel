import os
import tempfile
import threading
from paddleocr import PaddleOCR

# PaddleOCR's underlying inference engine is a single shared native object per
# instance and is not safe to call from multiple threads at once (concurrent
# `.ocr()` calls have been observed to hang/corrupt results). Since one
# OCRProcessor is reused across all requests, serialize access here.
_OCR_LOCK = threading.Lock()


class OCRProcessor:

    def __init__(self):
        self.ocr = self._build_ocr()

    def _build_ocr(self):
        try:
            return PaddleOCR(
                use_angle_cls=True,
                lang="en",
                use_gpu=False,
            )
        except (TypeError, ValueError) as exc:
            if "Unknown argument" in str(exc) or "unexpected keyword argument" in str(exc):
                return PaddleOCR(use_angle_cls=True, lang="en")
            raise

    def extract_text(self, image_source):
        temp_path = None
        if isinstance(image_source, (bytes, bytearray)):
            with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as temp_file:
                temp_file.write(image_source)
                temp_path = temp_file.name
            image_path = temp_path
        else:
            image_path = image_source

        try:
            with _OCR_LOCK:
                result = self.ocr.ocr(image_path, cls=True)
        finally:
            if temp_path:
                try:
                    os.remove(temp_path)
                except OSError:
                    pass

        extracted_text = []
        for line in result[0]:
            bbox = line[0]
            text = line[1][0]
            score = line[1][1]
            extracted_text.append({'bbox': bbox, 'text': text, 'score': score})
        return extracted_text
