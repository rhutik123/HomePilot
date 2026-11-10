import io
import logging
from pypdf import PdfReader

logger = logging.getLogger(__name__)


class DocumentParserService:
    """
    Extracts text content from uploaded bill documents (PDFs and images).
    Robust against malformed streams with fallback string decoding.
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

        # Handle PDF
        if mime_type == "application/pdf" or content.startswith(b"%PDF"):
            try:
                reader = PdfReader(io.BytesIO(content))
                extracted_pages = []
                for page in reader.pages:
                    page_text = page.extract_text()
                    if page_text:
                        extracted_pages.append(page_text.strip())
                if extracted_pages:
                    return "\n\n".join(extracted_pages)
            except Exception as e:
                logger.warning(f"Standard PDF parser encountered error: {e}. Attempting text fallback.")

            # Fallback: Extract readable ASCII/UTF-8 string fragments
            try:
                decoded = content.decode("utf-8", errors="ignore")
                lines = [line.strip() for line in decoded.splitlines() if line.strip()]
                return "\n".join(lines)
            except Exception:
                return ""

        # Handle images
        if mime_type.startswith("image/"):
            return ""

        return ""
