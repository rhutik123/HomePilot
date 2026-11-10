import io
import logging
from abc import ABC, abstractmethod
from typing import Optional, Tuple
from PIL import Image
from pypdf import PdfReader

logger = logging.getLogger(__name__)


class BaseOCRService(ABC):
    """Abstract OCR service interface."""
    @abstractmethod
    def extract_text_from_image(self, image_bytes: bytes) -> str:
        """Extracts text from raw image bytes."""
        pass


class LocalTesseractOCRService(BaseOCRService):
    """
    Local OCR engine using Tesseract via pytesseract if installed.
    Gracefully detects whether the system Tesseract binary is present.
    """
    def __init__(self):
        self._is_available = None

    def is_available(self) -> bool:
        if self._is_available is None:
            try:
                import pytesseract
                # Test call to verify binary
                pytesseract.get_tesseract_version()
                self._is_available = True
            except Exception:
                self._is_available = False
        return self._is_available

    def extract_text_from_image(self, image_bytes: bytes) -> str:
        if not self.is_available():
            logger.info("Tesseract binary not available locally. Falling back to visual preprocessing.")
            return ""

        try:
            import pytesseract
            image = Image.open(io.BytesIO(image_bytes))
            # Convert to grayscale for cleaner OCR accuracy
            gray_image = image.convert("L")
            text = pytesseract.image_to_string(gray_image)
            return text.strip()
        except Exception as e:
            logger.warning(f"Local Tesseract OCR processing failed: {e}")
            return ""


class DocumentParserEngine:
    """
    Unified document parsing engine.
    Separates embedded digital text extraction from OCR image processing.
    """
    def __init__(self, ocr_service: Optional[BaseOCRService] = None):
        self.ocr_service = ocr_service or LocalTesseractOCRService()

    def parse_document(self, file_content: bytes, mime_type: str = "") -> Tuple[str, str]:
        """
        Parses document bytes.
        Returns tuple of (extracted_text, extraction_method).
        Method can be: 'EMBEDDED_PDF', 'SCANNED_PDF_OCR', 'IMAGE_OCR', or 'FALLBACK_RAW'.
        """
        if not file_content:
            return "", "EMPTY"

        # 1. Digital PDF or Scanned PDF
        if mime_type == "application/pdf" or file_content.startswith(b"%PDF"):
            try:
                reader = PdfReader(io.BytesIO(file_content))
                pages_text = []
                for idx, page in enumerate(reader.pages):
                    text = page.extract_text()
                    if text and text.strip():
                        pages_text.append(text.strip())

                combined_text = "\n\n".join(pages_text).strip()
                # If embedded text is found, return it
                if len(combined_text) > 30:
                    return combined_text, "EMBEDDED_PDF"

                logger.info("PDF contains sparse or no embedded text. Document is likely a scanned PDF.")
                # Attempt OCR on scanned PDF pages if OCR service is available
                # (In future Phase 1 enhancements, pdf2image renders pages to images for OCR)
                if combined_text:
                    return combined_text, "EMBEDDED_PDF"

                return "", "SCANNED_PDF_NO_TEXT"
            except Exception as e:
                logger.warning(f"Error parsing PDF with pypdf: {e}")
                return "", "PDF_PARSE_ERROR"

        # 2. Image files (JPEG, PNG, WEBP)
        if mime_type.startswith("image/") or file_content.startswith((b"\xff\xd8\xff", b"\x89PNG", b"RIFF")):
            try:
                # Validate image integrity using Pillow
                image = Image.open(io.BytesIO(file_content))
                image.verify()
                
                # Run OCR
                ocr_text = self.ocr_service.extract_text_from_image(file_content)
                if ocr_text:
                    return ocr_text, "IMAGE_OCR"
                
                return "", "IMAGE_NO_OCR_OUTPUT"
            except Exception as e:
                logger.warning(f"Error processing image file: {e}")
                return "", "IMAGE_PARSE_ERROR"

        return "", "UNSUPPORTED_FORMAT"


_parser_singleton: Optional[DocumentParserEngine] = None


def get_document_parser() -> DocumentParserEngine:
    global _parser_singleton
    if _parser_singleton is None:
        _parser_singleton = DocumentParserEngine()
    return _parser_singleton
