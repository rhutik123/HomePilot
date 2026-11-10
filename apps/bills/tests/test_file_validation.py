import io
import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.bills.validators import validate_bill_document, MAX_FILE_SIZE_BYTES


class TestFileValidation:
    def test_valid_pdf_passes(self):
        """A valid PDF file signature passes validation."""
        pdf_content = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF"
        file_obj = SimpleUploadedFile("bill.pdf", pdf_content, content_type="application/pdf")
        assert validate_bill_document(file_obj) is True

    def test_valid_jpeg_passes(self):
        """A valid JPEG file signature passes validation."""
        jpeg_content = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
        file_obj = SimpleUploadedFile("bill.jpg", jpeg_content, content_type="image/jpeg")
        assert validate_bill_document(file_obj) is True

    def test_valid_png_passes(self):
        """A valid PNG file signature passes validation."""
        png_content = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
        file_obj = SimpleUploadedFile("bill.png", png_content, content_type="image/png")
        assert validate_bill_document(file_obj) is True

    def test_valid_webp_passes(self):
        """A valid WebP file signature (RIFF....WEBP) passes validation."""
        webp_content = b"RIFF\x24\x00\x00\x00WEBPVP8 \x18\x00\x00\x00"
        file_obj = SimpleUploadedFile("bill.webp", webp_content, content_type="image/webp")
        assert validate_bill_document(file_obj) is True

    def test_spoofed_riff_non_webp_rejected(self):
        """A file with .webp extension starting with RIFF but with non-WEBP container (e.g. WAVE) is rejected."""
        riff_wave_content = b"RIFF\x24\x00\x00\x00WAVEfmt \x10\x00\x00\x00"
        file_obj = SimpleUploadedFile("fake.webp", riff_wave_content, content_type="image/webp")
        with pytest.raises(ValidationError) as exc:
            validate_bill_document(file_obj)
        assert "File content signature does not match" in str(exc.value)

    def test_disallowed_extension_rejected(self):
        """Disallowed extensions like .exe or .txt are rejected."""
        file_obj = SimpleUploadedFile("malware.exe", b"%PDF-1.4 Fake", content_type="application/octet-stream")
        with pytest.raises(ValidationError) as exc:
            validate_bill_document(file_obj)
        assert "Unsupported file extension" in str(exc.value)

    def test_spoofed_file_content_rejected(self):
        """A file with .pdf extension but invalid header bytes is rejected."""
        fake_content = b"This is just plain text masquerading as a PDF file."
        file_obj = SimpleUploadedFile("fake.pdf", fake_content, content_type="application/pdf")
        with pytest.raises(ValidationError) as exc:
            validate_bill_document(file_obj)
        assert "File content signature does not match" in str(exc.value)

    def test_oversized_file_rejected(self):
        """A file exceeding 10MB is rejected."""
        large_size = MAX_FILE_SIZE_BYTES + 1024
        # Create a mock file object with large size without allocating 10MB in memory
        file_obj = SimpleUploadedFile("large.pdf", b"%PDF-1.4" + b"0" * 100)
        file_obj.size = large_size
        with pytest.raises(ValidationError) as exc:
            validate_bill_document(file_obj)
        assert "exceeds the 10MB limit" in str(exc.value)

    def test_empty_file_rejected(self):
        """Empty files are rejected."""
        file_obj = SimpleUploadedFile("empty.pdf", b"", content_type="application/pdf")
        with pytest.raises(ValidationError) as exc:
            validate_bill_document(file_obj)
        assert "Uploaded file is empty" in str(exc.value)
