from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch
import pytest
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework import status

from apps.households.models import Household, HouseholdMembership
from apps.bills.models import Bill, BillDocument, BillStatus, BillCategory

User = get_user_model()


@pytest.mark.django_db
class TestPhase1BillCreationWebFlow:
    def setup_method(self):
        self.client = APIClient()
        self.owner = User.objects.create_user(
            username="clara_web",
            email="clara_web@example.com",
            password="SecurePassword123!",
            first_name="Clara",
            last_name="Web",
        )
        self.member = User.objects.create_user(
            username="david_web",
            email="david_web@example.com",
            password="SecurePassword123!",
            first_name="David",
            last_name="Web",
        )

        self.household = Household.objects.create(name="Sunset Residency", created_by=self.owner)
        HouseholdMembership.objects.create(household=self.household, user=self.owner, role=HouseholdMembership.Role.OWNER)
        HouseholdMembership.objects.create(household=self.household, user=self.member, role=HouseholdMembership.Role.MEMBER)

        self.client.force_login(self.owner)
        session = self.client.session
        session["active_household_id"] = self.household.id
        session.save()

    def test_landing_page_renders_both_options(self):
        """Add Bill page presents Option A (Upload Bill) and Option B (Manual Entry) tabs."""
        response = self.client.get("/bills/upload/")
        assert response.status_code == status.HTTP_200_OK
        content = response.content.decode("utf-8")
        assert "Option A: Upload Digital Bill" in content
        assert "Option B: Enter Bill Manually" in content
        assert "Supports PDF, scanned PDF, PNG, JPG, JPEG, and WEBP" in content
        assert "No file upload required" in content

    def test_direct_manual_bill_creation_web_success(self):
        """User can submit manual bill form without uploading any file."""
        today = timezone.localdate()
        due_date = today + timedelta(days=20)

        payload = {
            "action": "manual_bill",
            "provider_name": "Lawn Care Service",
            "category": BillCategory.OTHER,
            "amount": "1400.00",
            "currency": "INR",
            "due_date": str(due_date),
            "billing_period": "November 2026",
            "assigned_to": str(self.member.id),
            "notes": "Paid in cash to gardener; handwritten receipt.",
        }

        response = self.client.post("/bills/upload/", payload, follow=True)
        assert response.status_code == status.HTTP_200_OK

        bill = Bill.objects.filter(provider_name="Lawn Care Service").first()
        assert bill is not None
        assert bill.amount == Decimal("1400.00")
        assert bill.assigned_to == self.member
        assert bill.status == BillStatus.PENDING
        # No fake document created
        assert bill.documents.count() == 0

    def test_manual_bill_creation_validation_error_preserves_form_input(self):
        """Invalid manual entry shows meaningful error message and retains form inputs."""
        payload = {
            "action": "manual_bill",
            "provider_name": "Invalid Utility",
            "category": BillCategory.OTHER,
            "amount": "-500.00",  # Invalid negative amount
            "currency": "INR",
            "due_date": "2026-11-25",
        }

        response = self.client.post("/bills/upload/", payload)
        assert response.status_code == status.HTTP_200_OK
        content = response.content.decode("utf-8")
        assert "greater than zero" in content
        # Input value is preserved
        assert "Invalid Utility" in content
        assert not Bill.objects.filter(provider_name="Invalid Utility").exists()

    def test_document_upload_extraction_failure_preserves_document_and_shows_manual_fallback(self):
        """When AI/OCR extraction fails, document is NOT deleted and review form opens with manual fallback warning."""
        pdf_bytes = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF"
        upload_file = SimpleUploadedFile("corrupted_scan.pdf", pdf_bytes, content_type="application/pdf")

        # Simulate extraction provider failure
        with patch("apps.bills.services.document_parser.DocumentParserService.extract_text", side_effect=RuntimeError("OCR engine timeout")):
            response = self.client.post(
                "/bills/upload/",
                {"action": "upload_file", "bill_file": upload_file},
            )

        assert response.status_code == status.HTTP_200_OK
        content = response.content.decode("utf-8")

        # Preserves the uploaded document
        doc = BillDocument.objects.filter(file_name="corrupted_scan.pdf").first()
        assert doc is not None
        assert doc.extraction_status == BillDocument.ExtractionStatus.FAILED

        # User is presented with the review & fallback screen
        assert "Could not automatically extract all bill details. You can enter or correct the information manually." in content
        assert "Verify Bill Details Before Saving" in content

        # User can now complete details manually and confirm
        today = timezone.localdate()
        confirm_payload = {
            "action": "confirm_bill",
            "doc_id": doc.id,
            "provider_name": "Recovered Water Bill",
            "category": BillCategory.WATER,
            "amount": "650.00",
            "currency": "INR",
            "due_date": str(today + timedelta(days=10)),
        }
        confirm_response = self.client.post("/bills/upload/", confirm_payload, follow=True)
        assert confirm_response.status_code == status.HTTP_200_OK

        bill = Bill.objects.filter(provider_name="Recovered Water Bill").first()
        assert bill is not None
        assert bill.amount == Decimal("650.00")
        # Document is properly linked
        doc.refresh_from_db()
        assert doc.bill == bill

    def test_bill_edit_view_loads_and_updates(self):
        """User can view and edit an existing bill via the web UI."""
        today = timezone.localdate()
        bill = Bill.objects.create(
            household=self.household,
            uploaded_by=self.owner,
            provider_name="Original Broadband",
            amount=Decimal("999.00"),
            due_date=today + timedelta(days=8),
            status=BillStatus.PENDING,
        )

        # GET edit form
        get_resp = self.client.get(f"/bills/{bill.id}/edit/")
        assert get_resp.status_code == status.HTTP_200_OK
        assert "Original Broadband" in get_resp.content.decode("utf-8")

        # POST update
        new_due = today + timedelta(days=15)
        post_resp = self.client.post(
            f"/bills/{bill.id}/edit/",
            {
                "provider_name": "Upgraded Fiber",
                "category": BillCategory.INTERNET,
                "amount": "1299.00",
                "currency": "INR",
                "due_date": str(new_due),
                "assigned_to": str(self.member.id),
                "notes": "Plan upgraded to 500Mbps",
            },
            follow=True,
        )
        assert post_resp.status_code == status.HTTP_200_OK

        bill.refresh_from_db()
        assert bill.provider_name == "Upgraded Fiber"
        assert bill.amount == Decimal("1299.00")
        assert bill.outstanding_balance == Decimal("1299.00")
        assert bill.assigned_to == self.member
        assert bill.due_date == new_due
