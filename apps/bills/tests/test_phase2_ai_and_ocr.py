import io
import json
from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch
import pytest
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from rest_framework.test import APIClient
from rest_framework import status
from PIL import Image

from apps.households.models import Household, HouseholdMembership
from apps.bills.models import Bill, BillDocument, BillCategory
from apps.bills.services.extraction import (
    GeminiExtractionProvider,
    MockExtractionProvider,
    get_extraction_provider,
    AIExtractionError,
    AIExtractionConfigurationError,
)
from apps.bills.services.schemas import ExtractedBillData
from apps.documents.services.ocr import DocumentParserEngine, LocalTesseractOCRService

User = get_user_model()


class TestGeminiExtractionProvider:
    def test_gemini_provider_requires_api_key_no_silent_mock(self):
        """Requirement 15: If Gemini is selected without an API key, fail explicitly rather than silently falling back to mock."""
        provider = GeminiExtractionProvider(api_key="")
        with pytest.raises(AIExtractionConfigurationError) as exc_info:
            provider.extract("Sample bill text from document")
        assert "GEMINI_API_KEY is not configured" in str(exc_info.value)

    def test_gemini_provider_successful_structured_extraction(self):
        """Gemini extraction parses structured JSON into typed ExtractedBillData with provenance and confidence scores."""
        mock_response_json = {
            "provider_name": "Adani Electricity Mumbai",
            "category": "ELECTRICITY",
            "total_amount": "3450.75",
            "currency": "INR",
            "consumer_account_number": "CA-909210",
            "bill_number": "INV-2026-909",
            "billing_period": "October 2026",
            "billing_period_start": "2026-10-01",
            "billing_period_end": "2026-10-31",
            "issue_date": "2026-11-02",
            "due_date": "2026-11-20",
            "previous_balance": "0.00",
            "current_charges": "3200.00",
            "taxes_and_fees": "250.75",
            "field_confidences": {
                "provider_name": 0.98,
                "total_amount": 0.99,
                "due_date": 0.95,
            },
        }

        mock_response = MagicMock()
        mock_response.text = json.dumps(mock_response_json)

        mock_client = MagicMock()
        mock_client.models.generate_content.return_value = mock_response

        with patch("google.genai.Client", return_value=mock_client):
            provider = GeminiExtractionProvider(api_key="valid-test-key")
            result = provider.extract("Sample raw OCR text of Adani electricity bill")

        assert result.provider_name == "Adani Electricity Mumbai"
        assert result.category == "ELECTRICITY"
        assert result.amount == Decimal("3450.75")
        assert result.currency == "INR"
        assert result.due_date == date(2026, 11, 20)
        assert result.issue_date == date(2026, 11, 2)
        assert result.consumer_account_number == "CA-909210"
        assert result.bill_number == "INV-2026-909"
        assert result.provider_used == "gemini"
        assert result.model_used == "gemini-2.5-flash"
        assert result.field_confidences["total_amount"] == 0.99
        assert result.overall_confidence > 0.9

    def test_gemini_provider_handles_invalid_json(self):
        """When Gemini returns non-JSON or corrupted text, raise AIExtractionError."""
        mock_response = MagicMock()
        mock_response.text = "This is not valid json at all!"

        mock_client = MagicMock()
        mock_client.models.generate_content.return_value = mock_response

        with patch("google.genai.Client", return_value=mock_client):
            provider = GeminiExtractionProvider(api_key="valid-test-key")
            with pytest.raises(AIExtractionError) as exc_info:
                provider.extract("Some bill text")
            assert "invalid JSON" in str(exc_info.value)

    def test_gemini_provider_handles_api_exception(self):
        """When Gemini API raises network, rate limit, or timeout errors, raise AIExtractionError without exposing key."""
        mock_client = MagicMock()
        mock_client.models.generate_content.side_effect = RuntimeError("429 Resource has been exhausted (rate limit)")

        with patch("google.genai.Client", return_value=mock_client):
            provider = GeminiExtractionProvider(api_key="valid-test-key")
            with pytest.raises(AIExtractionError) as exc_info:
                provider.extract("Some bill text")
            assert "rate limit" in str(exc_info.value)
            # Never expose secret API key in exception message
            assert "valid-test-key" not in str(exc_info.value)

    def test_get_extraction_provider_respects_backend_setting(self):
        """Factory function returns real Gemini provider or Mock provider based on BILL_EXTRACTION_BACKEND."""
        with override_settings(BILL_EXTRACTION_BACKEND="mock"):
            provider = get_extraction_provider()
            assert isinstance(provider, MockExtractionProvider)

        with override_settings(BILL_EXTRACTION_BACKEND="gemini", GEMINI_API_KEY="test-key"):
            provider = get_extraction_provider()
            assert isinstance(provider, GeminiExtractionProvider)
            assert provider.api_key == "test-key"


class TestOCRAndDocumentParsing:
    def test_local_tesseract_ocr_gracefully_handles_missing_binary(self):
        """When Tesseract binary is missing from PATH, service reports unavailable and returns empty string without crashing."""
        ocr = LocalTesseractOCRService()
        with patch.object(ocr, "is_available", return_value=False):
            result = ocr.extract_text_from_image(b"fake_image_bytes")
            assert result == ""

    def test_image_preprocessing_converts_to_grayscale(self):
        """Image OCR converts input image to grayscale ('L' mode) for optimal OCR contrast."""
        import sys
        img = Image.new("RGB", (100, 100), color="red")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        image_bytes = buf.getvalue()

        ocr = LocalTesseractOCRService()
        mock_pytess = MagicMock()
        mock_pytess.image_to_string.return_value = "EXTRACTED OCR TEXT"

        with patch.dict(sys.modules, {"pytesseract": mock_pytess}):
            with patch.object(ocr, "is_available", return_value=True):
                text = ocr.extract_text_from_image(image_bytes)
                assert text == "EXTRACTED OCR TEXT"
                assert mock_pytess.image_to_string.called
                called_img = mock_pytess.image_to_string.call_args[0][0]
                assert called_img.mode == "L"  # Grayscale verification


@pytest.mark.django_db
class TestDuplicateDetectionAndMultipleBillUpload:
    def setup_method(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="rachel_p2",
            email="rachel_p2@example.com",
            password="SecurePassword123!",
        )
        self.household = Household.objects.create(name="Phase 2 Household", created_by=self.user)
        HouseholdMembership.objects.create(
            household=self.household,
            user=self.user,
            role=HouseholdMembership.Role.OWNER,
        )
        self.client.force_login(self.user)
        session = self.client.session
        session["active_household_id"] = self.household.id
        session.save()

    def test_checksum_based_duplicate_detection(self):
        """Uploading identical document content in the same household flags is_possible_duplicate."""
        file_bytes = b"%PDF-1.4\nUnique Receipt Content 12345\n%%EOF"
        file1 = SimpleUploadedFile("first_upload.pdf", file_bytes, content_type="application/pdf")
        file2 = SimpleUploadedFile("second_upload.pdf", file_bytes, content_type="application/pdf")

        # 1. First upload
        resp1 = self.client.post("/bills/upload/", {"action": "upload_file", "bill_file": file1})
        assert resp1.status_code == status.HTTP_200_OK

        doc1 = BillDocument.objects.get(file_name="first_upload.pdf")
        assert doc1.file_hash != ""
        assert doc1.is_possible_duplicate is False

        # 2. Second upload with identical bytes
        resp2 = self.client.post("/bills/upload/", {"action": "upload_file", "bill_file": file2})
        assert resp2.status_code == status.HTTP_200_OK

        doc2 = BillDocument.objects.get(file_name="second_upload.pdf")
        assert doc2.file_hash == doc1.file_hash
        assert doc2.is_possible_duplicate is True
        assert "Identical file content previously uploaded" in doc2.duplicate_warning

    def test_batch_upload_multiple_documents(self):
        """Users can upload multiple bill documents at once, each creating an independent record."""
        pdf1 = SimpleUploadedFile("bill_one.pdf", b"%PDF-1.4\nBill 1\n%%EOF", content_type="application/pdf")
        pdf2 = SimpleUploadedFile("bill_two.pdf", b"%PDF-1.4\nBill 2\n%%EOF", content_type="application/pdf")

        response = self.client.post(
            "/bills/upload/",
            {"action": "upload_file", "bill_files": [pdf1, pdf2]},
        )
        assert response.status_code == status.HTTP_200_OK
        content = response.content.decode("utf-8")
        assert "Successfully uploaded and analyzed 2 documents" in content

        # Verify separate records created with independent status
        assert BillDocument.objects.filter(file_name="bill_one.pdf").exists()
        assert BillDocument.objects.filter(file_name="bill_two.pdf").exists()
        assert BillDocument.objects.filter(household=self.household, bill__isnull=True).count() >= 2
