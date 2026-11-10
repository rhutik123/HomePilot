import io
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch, MagicMock
import pytest
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework import status
from pypdf import PdfWriter

from apps.households.models import Household, HouseholdMembership
from apps.bills.models import Bill, BillStatus
from apps.documents.models import Document, DocumentProcessingJob, ExtractionResult, BillDocumentLink
from apps.documents.services.storage import get_document_storage, LocalSecureDocumentStorage
from apps.documents.services.ocr import DocumentParserEngine, LocalTesseractOCRService
from apps.bills.services.schemas import ExtractedBillData
from apps.bills.services.extraction import GeminiExtractionProvider, MockExtractionProvider

User = get_user_model()


def generate_test_pdf(text: str) -> bytes:
    """Helper to generate a valid PDF byte string."""
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    buffer = io.BytesIO()
    writer.write(buffer)
    # Append readable ASCII marker for testing
    return buffer.getvalue() + f"\n{text}\n".encode("utf-8")


@pytest.mark.django_db
class TestDocumentManagementAndExtraction:
    def setup_method(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="doc_owner",
            email="doc_owner@example.com",
            password="SecurePassword123!",
            first_name="Doc",
            last_name="Owner",
        )
        self.stranger = User.objects.create_user(
            username="doc_stranger",
            email="doc_stranger@example.com",
            password="SecurePassword123!",
        )

        self.household = Household.objects.create(name="Document Testing Household", created_by=self.user)
        HouseholdMembership.objects.create(household=self.household, user=self.user, role=HouseholdMembership.Role.OWNER)

        self.stranger_household = Household.objects.create(name="Stranger Household", created_by=self.stranger)
        HouseholdMembership.objects.create(household=self.stranger_household, user=self.stranger, role=HouseholdMembership.Role.OWNER)

        self.client.force_authenticate(user=self.user)

    def test_document_upload_computes_sha256_and_queues_job(self):
        """Uploading a document computes SHA-256 hash, queues async job, and extracts data."""
        pdf_bytes = generate_test_pdf("Provider: Tata Power\nAmount: 2450.00\nDue Date: 2026-11-25\nAccount: ELE-99221")
        uploaded_file = SimpleUploadedFile("tata_bill.pdf", pdf_bytes, content_type="application/pdf")

        response = self.client.post(
            "/api/v1/documents/upload/",
            {"household_id": self.household.id, "file": uploaded_file},
            format="multipart",
        )
        assert response.status_code == status.HTTP_201_CREATED
        assert "document" in response.data
        assert "job_id" in response.data

        doc_id = response.data["document"]["id"]
        document = Document.objects.get(id=doc_id)
        assert document.file_hash is not None
        assert len(document.file_hash) == 64  # Valid SHA-256 hex string

        # Because Celery runs in eager mode during testing, job should complete immediately
        job = DocumentProcessingJob.objects.get(id=response.data["job_id"])
        assert job.status == DocumentProcessingJob.Status.SUCCESS

        extraction = ExtractionResult.objects.filter(document=document).first()
        assert extraction is not None
        assert extraction.extracted_data["provider_name"] == "Tata Power"
        assert extraction.extracted_data["amount"] == "2450.00"
        assert extraction.confidence_score > Decimal("0.00")

    def test_duplicate_file_upload_flagged(self):
        """Uploading identical content within the same household detects duplicate file."""
        pdf_bytes = generate_test_pdf("Provider: Airtel\nAmount: 999.00\nDue Date: 2026-12-01")
        file1 = SimpleUploadedFile("airtel1.pdf", pdf_bytes, content_type="application/pdf")
        file2 = SimpleUploadedFile("airtel2.pdf", pdf_bytes, content_type="application/pdf")

        # First upload
        res1 = self.client.post("/api/v1/documents/upload/", {"household_id": self.household.id, "file": file1}, format="multipart")
        assert res1.status_code == status.HTTP_201_CREATED
        assert res1.data["is_duplicate_file"] is False

        # Second upload with identical file hash
        res2 = self.client.post("/api/v1/documents/upload/", {"household_id": self.household.id, "file": file2}, format="multipart")
        assert res2.status_code == status.HTTP_201_CREATED
        assert res2.data["is_duplicate_file"] is True

    def test_duplicate_bill_detection_by_provider_amount_due(self):
        """Detects if an existing confirmed Bill matches the newly extracted document details."""
        today = timezone.localdate()
        target_due = today + timedelta(days=7)

        # Pre-existing confirmed bill
        Bill.objects.create(
            household=self.household,
            uploaded_by=self.user,
            provider_name="City Gas",
            amount=Decimal("850.00"),
            due_date=target_due,
            status=BillStatus.PENDING,
        )

        pdf_bytes = generate_test_pdf(f"Provider: City Gas\nAmount: 850.00\nDue Date: {target_due.isoformat()}")
        file_obj = SimpleUploadedFile("gas_bill.pdf", pdf_bytes, content_type="application/pdf")

        response = self.client.post(
            "/api/v1/documents/upload/",
            {"household_id": self.household.id, "file": file_obj},
            format="multipart",
        )
        assert response.status_code == status.HTTP_201_CREATED

        doc_id = response.data["document"]["id"]
        extraction = ExtractionResult.objects.get(document_id=doc_id)
        assert extraction.possible_duplicate is True
        assert "City Gas" in extraction.duplicate_reason
        assert "850.00" in extraction.duplicate_reason

    def test_secure_document_download_tenant_isolation(self):
        """Authenticated household member can download; unauthorized stranger gets 404."""
        pdf_bytes = generate_test_pdf("Sensitive Family Bill Content")
        uploaded_file = SimpleUploadedFile("confidential.pdf", pdf_bytes, content_type="application/pdf")

        upload_res = self.client.post("/api/v1/documents/upload/", {"household_id": self.household.id, "file": uploaded_file}, format="multipart")
        doc_id = upload_res.data["document"]["id"]

        # Member download -> 200 OK
        res_member = self.client.get(f"/api/v1/documents/{doc_id}/download/")
        assert res_member.status_code == status.HTTP_200_OK

        # Stranger download -> 404 Not Found (strict tenant scoping)
        self.client.force_authenticate(user=self.stranger)
        res_stranger = self.client.get(f"/api/v1/documents/{doc_id}/download/")
        assert res_stranger.status_code == status.HTTP_404_NOT_FOUND

    def test_document_confirm_creates_bill_and_link(self):
        """Confirming reviewed data creates Bill and BillDocumentLink record."""
        pdf_bytes = generate_test_pdf("Provider: BSES\nAmount: 1400.00\nDue Date: 2026-11-20")
        uploaded_file = SimpleUploadedFile("bses_bill.pdf", pdf_bytes, content_type="application/pdf")

        upload_res = self.client.post("/api/v1/documents/upload/", {"household_id": self.household.id, "file": uploaded_file}, format="multipart")
        doc_id = upload_res.data["document"]["id"]

        confirm_payload = {
            "provider_name": "BSES Rajdhani Power Limited",
            "category": "ELECTRICITY",
            "amount": "1400.00",
            "currency": "INR",
            "due_date": "2026-11-20",
            "billing_period": "November 2026",
            "notes": "Reviewed and verified from e-bill.",
        }

        confirm_res = self.client.post(f"/api/v1/documents/{doc_id}/confirm/", confirm_payload, format="json")
        assert confirm_res.status_code == status.HTTP_201_CREATED

        bill_id = confirm_res.data["bill"]["id"]
        bill = Bill.objects.get(id=bill_id)
        assert bill.provider_name == "BSES Rajdhani Power Limited"
        assert bill.amount == Decimal("1400.00")

        # Verify through-link
        assert BillDocumentLink.objects.filter(bill=bill, document_id=doc_id).exists()

    def test_storage_abstraction(self):
        """Tests BaseDocumentStorage saving, reading, existence, and SHA-256 calculation."""
        storage = get_document_storage()
        sample_bytes = b"Storage abstraction test payload 123"
        computed_hash = storage.compute_sha256(sample_bytes)
        assert len(computed_hash) == 64

        saved_path = storage.save("test_sample.bin", io.BytesIO(sample_bytes))
        assert storage.exists(saved_path)

        with storage.open(saved_path, "rb") as f:
            content = f.read()
            assert content == sample_bytes

        storage.delete(saved_path)
        assert not storage.exists(saved_path)

    def test_gemini_extraction_provider_with_mocked_api(self):
        """Tests that GeminiExtractionProvider invokes Gemini API and normalizes structured response."""
        sample_ai_json = """
        {
            "provider_name": "Tata Power Renewable",
            "category": "ELECTRICITY",
            "total_amount": "3150.50",
            "currency": "INR",
            "consumer_account_number": "CA-88991122",
            "bill_number": "INV-2026-99",
            "billing_period": "October 2026",
            "billing_period_start": "2026-10-01",
            "billing_period_end": "2026-10-31",
            "issue_date": "2026-11-02",
            "due_date": "2026-11-22",
            "previous_balance": "0.00",
            "current_charges": "3150.50",
            "taxes_and_fees": "150.00",
            "field_confidences": {
                "provider_name": 0.98,
                "total_amount": 0.99,
                "due_date": 0.95
            }
        }
        """
        mock_response = MagicMock()
        mock_response.text = sample_ai_json

        mock_client = MagicMock()
        mock_client.models.generate_content.return_value = mock_response

        provider = GeminiExtractionProvider(api_key="test-mock-gemini-key")

        with patch("google.genai.Client", return_value=mock_client):
            extracted = provider.extract("Sample Bill Raw Text", mime_type="application/pdf")

        assert extracted.provider_name == "Tata Power Renewable"
        assert extracted.category == "ELECTRICITY"
        assert extracted.amount == Decimal("3150.50")
        assert extracted.due_date == date(2026, 11, 22)
        assert extracted.issue_date == date(2026, 11, 2)
        assert extracted.consumer_account_number == "CA-88991122"
        assert extracted.bill_number == "INV-2026-99"
        assert extracted.overall_confidence > 0.9
        assert extracted.missing_fields == []

    def test_gemini_extraction_provider_fallback_on_api_error(self):
        """Tests that GeminiExtractionProvider falls back gracefully to rule-based mock when allow_fallback is enabled."""
        mock_client = MagicMock()
        mock_client.models.generate_content.side_effect = RuntimeError("QuotaExceeded or NetworkTimeout")

        provider = GeminiExtractionProvider(api_key="test-mock-gemini-key", allow_fallback=True)

        with patch("google.genai.Client", return_value=mock_client):
            # Input contains text parseable by fallback mock
            extracted = provider.extract("Provider: Tata Power\nAmount: 1800.00\nDue Date: 2026-12-15")

        assert extracted.provider_name == "Tata Power"
        assert extracted.amount == Decimal("1800.00")
        assert extracted.due_date == date(2026, 12, 15)
        assert "Fallback extractor" in (extracted.confidence_notes or "")
