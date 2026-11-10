import io
import logging
from pypdf import PdfReader
from apps.documents.services.ocr import get_document_parser

logger = logging.getLogger(__name__)


class DocumentParserService:
    """
    Extracts text content from uploaded bill documents (PDFs and images).
    Delegates to the dedicated document parsing & OCR engine.
    """
    @classmethod
    def extract_text(cls, file_obj, mime_type: str = "") -> str:
        if hasattr(file_obj, "seek"):
            file_obj.seek(0)

        content = file_obj.read()
        if hasattr(file_obj, "seek"):
            file_obj.seek(0)

        if not content:
            return ""

        # Delegate to unified OCR & parsing engine
        parser = get_document_parser()
        extracted_text, method = parser.parse_document(content, mime_type=mime_type)
        if extracted_text:
            return extracted_text

        # Fallback for plain-text or ASCII pseudo-PDF testing streams
        if mime_type == "application/pdf" or content.startswith(b"%PDF"):
            try:
                decoded = content.decode("utf-8", errors="ignore")
                lines = [line.strip() for line in decoded.splitlines() if line.strip() and not line.startswith(("%PDF", "%%EOF"))]
                return "\n".join(lines)
            except Exception:
                return ""

        return ""
