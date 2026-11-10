from datetime import date, timedelta
from decimal import Decimal
import io
import pytest
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIClient
from rest_framework import status
from pypdf import PdfWriter

from apps.households.models import Household, HouseholdMembership
from apps.bills.models import Bill, BillDocument, BillStatus
from apps.bills.services.schemas import ExtractedBillData
from apps.bills.services.document_parser import DocumentParserService
from apps.bills.services.extraction import MockExtractionProvider

User = get_user_model()


def create_sample_pdf(text_content: str) -> bytes:
    """Helper to generate a real, in-memory PDF containing text for testing."""
    writer = PdfWriter()
    # Adding a blank page with annotations/text
    writer.add_blank_page(width=72 * 8.5, height=72 * 11)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


class TestMockExtraction:
    def setup_method(self):
        self.provider = MockExtractionProvider()

    def test_complete_bill_extraction(self):
        """Mock provider parses provider, category, amount, due date, and reference."""
        sample_text = (
            "Tata Power Delhi Distribution Limited\n"
            "Bill for Electricity Service\n"
            "Account No: ELE-889922\n"
            "Billing Period: October 2026\n"
            "Total Amount Due: Rs. 2,450.00\n"
            "Payment Due Date: 2026-11-25\n"
        )
        result = self.provider.extract(sample_text)
        assert result.provider_name == "Tata Power"
        assert result.category == "ELECTRICITY"
        assert result.amount == Decimal("2450.00")
        assert result.due_date == date(2026, 11, 25)
        assert result.reference_number == "ELE-889922"
        assert result.missing_fields == []

    def test_missing_due_date_does_not_hallucinate(self):
        """Never invent a due date when one is not found in the bill text."""
        sample_text = (
            "Airtel Broadband\n"
            "Fiber High-Speed Internet Bill\n"
            "Amount Due: Rs. 1,099.00\n"
        )
        result = self.provider.extract(sample_text)
        assert result.provider_name == "Airtel Broadband"
        assert result.category == "INTERNET"
        assert result.amount == Decimal("1099.00")
        assert result.due_date is None
        assert "due_date" in result.missing_fields

    def test_empty_text_flags_all_required_missing(self):
        """Empty text flags provider_name, amount, and due_date as missing."""
        result = self.provider.extract("")
        assert result.provider_name is None
        assert result.amount is None
        assert result.due_date is None
        assert "provider_name" in result.missing_fields
        assert "amount" in result.missing_fields
        assert "due_date" in result.missing_fields


@pytest.mark.django_db
class TestExtractionAPIWorkflow:
    def setup_method(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="extractor",
            email="extractor@example.com",
            password="Password123!",
        )
        self.household = Household.objects.create(name="Family Nest", created_by=self.user)
        HouseholdMembership.objects.create(
            household=self.household,
            user=self.user,
            role=HouseholdMembership.Role.OWNER,
        )
        self.client.force_authenticate(user=self.user)

    def test_two_step_upload_extract_confirm_flow(self):
        """
        Step 1: Upload Document
        Step 2: Trigger Extract
        Step 3: Review, edit & Confirm Bill
        """
        # 1. Upload
        pdf_bytes = b"%PDF-1.4\nProvider: City Gas\nTotal Due: 850.00\nDue Date: 2026-11-30\n%%EOF"
        pdf_file = SimpleUploadedFile("gas_bill.pdf", pdf_bytes, content_type="application/pdf")
        upload_res = self.client.post(
            "/api/bills/documents/upload/",
            {"household_id": self.household.id, "file": pdf_file},
            format="multipart",
        )
        assert upload_res.status_code == status.HTTP_201_CREATED
        doc_id = upload_res.data["document"]["id"]

        # 2. Extract
        extract_res = self.client.post(f"/api/bills/documents/{doc_id}/extract/")
        assert extract_res.status_code == status.HTTP_200_OK
        assert "extracted_data" in extract_res.data

        # 3. Confirm & Save Bill with user corrections
        confirm_payload = {
            "provider_name": "City Gas Distribution Corp",  # user refined the name
            "category": "GAS",
            "amount": "850.00",
            "currency": "INR",
            "due_date": "2026-11-30",
            "notes": "Verified from physical paper bill.",
        }
        confirm_res = self.client.post(
            f"/api/bills/documents/{doc_id}/confirm/",
            confirm_payload,
            format="json",
        )
        assert confirm_res.status_code == status.HTTP_201_CREATED
        bill_data = confirm_res.data["bill"]
        assert bill_data["provider_name"] == "City Gas Distribution Corp"
        assert bill_data["amount"] == "850.00"

        # Verify database association
        doc = BillDocument.objects.get(id=doc_id)
        assert doc.bill is not None
        assert doc.bill.id == bill_data["id"]

    def test_confirm_rejects_invalid_data(self):
        """Confirming with invalid data (e.g. invalid date or negative amount) returns 400."""
        pdf_bytes = b"%PDF-1.4\nSample\n%%EOF"
        pdf_file = SimpleUploadedFile("bill.pdf", pdf_bytes, content_type="application/pdf")
        upload_res = self.client.post(
            "/api/bills/documents/upload/",
            {"household_id": self.household.id, "file": pdf_file},
            format="multipart",
        )
        doc_id = upload_res.data["document"]["id"]

        invalid_payload = {
            "provider_name": "Invalid Provider",
            "amount": "-10.00",  # invalid
            "due_date": "2026-11-30",
        }
        confirm_res = self.client.post(
            f"/api/bills/documents/{doc_id}/confirm/",
            invalid_payload,
            format="json",
        )
        assert confirm_res.status_code == status.HTTP_400_BAD_REQUEST
        assert "amount" in confirm_res.data
