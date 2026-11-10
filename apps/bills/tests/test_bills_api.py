from datetime import timedelta
import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIClient
from rest_framework import status

from apps.households.models import Household, HouseholdMembership
from apps.bills.models import Bill, BillDocument, BillStatus, BillCategory

User = get_user_model()


@pytest.mark.django_db
class TestBillsAPI:
    def setup_method(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="testuser",
            email="testuser@example.com",
            password="Password123!",
            first_name="Test",
            last_name="User",
        )
        self.family_member = User.objects.create_user(
            username="familymember",
            email="familymember@example.com",
            password="Password123!",
        )
        self.stranger = User.objects.create_user(
            username="stranger",
            email="stranger@example.com",
            password="Password123!",
        )

        # Create household and memberships
        self.household = Household.objects.create(name="Smith Home", created_by=self.user)
        HouseholdMembership.objects.create(household=self.household, user=self.user, role=HouseholdMembership.Role.OWNER)
        HouseholdMembership.objects.create(household=self.household, user=self.family_member, role=HouseholdMembership.Role.MEMBER)

        # Create another household for stranger
        self.other_household = Household.objects.create(name="Other Home", created_by=self.stranger)
        HouseholdMembership.objects.create(household=self.other_household, user=self.stranger, role=HouseholdMembership.Role.OWNER)

        self.client.force_authenticate(user=self.user)

    def test_create_bill_success(self):
        """User can manually create a valid bill for their household."""
        today = timezone.localdate()
        payload = {
            "household": self.household.id,
            "provider_name": "Tata Power",
            "category": "ELECTRICITY",
            "amount": "2450.00",
            "currency": "INR",
            "billing_period": "October 2026",
            "issue_date": str(today - timedelta(days=10)),
            "due_date": str(today + timedelta(days=15)),
            "reference_number": "ELE-998811",
            "assigned_to": self.family_member.id,
        }
        response = self.client.post("/api/bills/", payload, format="json")
        assert response.status_code == status.HTTP_201_CREATED
        assert response.data["provider_name"] == "Tata Power"
        assert response.data["amount"] == "2450.00"
        assert response.data["status"] == "PENDING"
        assert response.data["assigned_to"] == self.family_member.id

    def test_create_bill_rejects_negative_amount(self):
        """Negative or zero amounts are rejected."""
        today = timezone.localdate()
        payload = {
            "household": self.household.id,
            "provider_name": "Internet Provider",
            "amount": "-50.00",
            "due_date": str(today + timedelta(days=5)),
        }
        response = self.client.post("/api/bills/", payload, format="json")
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "amount" in response.data

    def test_create_bill_rejects_assignee_outside_household(self):
        """Assigning a bill to a user who is not a member of the household is rejected."""
        today = timezone.localdate()
        payload = {
            "household": self.household.id,
            "provider_name": "Water Board",
            "amount": "400.00",
            "due_date": str(today + timedelta(days=5)),
            "assigned_to": self.stranger.id,
        }
        response = self.client.post("/api/bills/", payload, format="json")
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "assigned_to" in response.data

    def test_create_bill_rejects_issue_date_after_due_date(self):
        """Issue date after due date is logically rejected."""
        today = timezone.localdate()
        payload = {
            "household": self.household.id,
            "provider_name": "Gas Provider",
            "amount": "900.00",
            "issue_date": str(today + timedelta(days=10)),
            "due_date": str(today + timedelta(days=2)),
        }
        response = self.client.post("/api/bills/", payload, format="json")
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "due_date" in response.data

    def test_mark_bill_as_paid(self):
        """Marking a bill as paid updates status and sets paid_at."""
        today = timezone.localdate()
        bill = Bill.objects.create(
            household=self.household,
            uploaded_by=self.user,
            provider_name="City Gas",
            amount="850.00",
            due_date=today + timedelta(days=5),
            status=BillStatus.PENDING,
        )
        response = self.client.post(f"/api/bills/{bill.id}/mark_paid/")
        assert response.status_code == status.HTTP_200_OK

        bill.refresh_from_db()
        assert bill.status == BillStatus.PAID
        assert bill.paid_at is not None

    def test_upcoming_and_overdue_filters(self):
        """Upcoming and overdue filter endpoints return correct subsets."""
        today = timezone.localdate()
        # Upcoming bill (due in 5 days)
        upcoming_bill = Bill.objects.create(
            household=self.household,
            provider_name="Upcoming Power",
            amount="1000.00",
            due_date=today + timedelta(days=5),
            status=BillStatus.PENDING,
        )
        # Overdue bill (due 3 days ago)
        overdue_bill = Bill.objects.create(
            household=self.household,
            provider_name="Overdue Water",
            amount="500.00",
            due_date=today - timedelta(days=3),
            status=BillStatus.PENDING,
        )
        # Paid bill
        paid_bill = Bill.objects.create(
            household=self.household,
            provider_name="Paid Internet",
            amount="1200.00",
            due_date=today + timedelta(days=2),
            status=BillStatus.PAID,
            paid_at=timezone.now(),
        )

        # Check upcoming
        res_upcoming = self.client.get("/api/bills/upcoming/")
        assert res_upcoming.status_code == status.HTTP_200_OK
        ids_upcoming = [b["id"] for b in res_upcoming.data]
        assert upcoming_bill.id in ids_upcoming
        assert overdue_bill.id not in ids_upcoming
        assert paid_bill.id not in ids_upcoming

        # Check overdue
        res_overdue = self.client.get("/api/bills/overdue/")
        assert res_overdue.status_code == status.HTTP_200_OK
        ids_overdue = [b["id"] for b in res_overdue.data]
        assert overdue_bill.id in ids_overdue
        assert upcoming_bill.id not in ids_overdue
        assert paid_bill.id not in ids_overdue

        # Check paid
        res_paid = self.client.get("/api/bills/paid/")
        assert res_paid.status_code == status.HTTP_200_OK
        ids_paid = [b["id"] for b in res_paid.data]
        assert paid_bill.id in ids_paid
        assert upcoming_bill.id not in ids_paid

    def test_strict_multi_tenant_isolation(self):
        """User cannot see, modify, or mark paid another household's bill."""
        today = timezone.localdate()
        stranger_bill = Bill.objects.create(
            household=self.other_household,
            uploaded_by=self.stranger,
            provider_name="Secret Bill",
            amount="3000.00",
            due_date=today + timedelta(days=5),
        )

        # Querying bill detail returns 404
        res_get = self.client.get(f"/api/bills/{stranger_bill.id}/")
        assert res_get.status_code == status.HTTP_404_NOT_FOUND

        # Trying to mark stranger's bill as paid returns 404
        res_mark = self.client.post(f"/api/bills/{stranger_bill.id}/mark_paid/")
        assert res_mark.status_code == status.HTTP_404_NOT_FOUND

    def test_document_upload_success(self):
        """Member can upload a bill document for their household."""
        pdf_content = b"%PDF-1.4\nSample bill document content\n%%EOF"
        pdf_file = SimpleUploadedFile("electric_bill.pdf", pdf_content, content_type="application/pdf")

        response = self.client.post(
            "/api/bills/documents/upload/",
            {"household_id": self.household.id, "file": pdf_file},
            format="multipart",
        )
        assert response.status_code == status.HTTP_201_CREATED
        doc_id = response.data["document"]["id"]
        assert BillDocument.objects.filter(id=doc_id, household=self.household).exists()

    def test_document_upload_rejected_for_unauthorized_household(self):
        """User cannot upload documents to a household they don't belong to."""
        pdf_content = b"%PDF-1.4\nSample bill content\n%%EOF"
        pdf_file = SimpleUploadedFile("bill.pdf", pdf_content, content_type="application/pdf")

        response = self.client.post(
            "/api/bills/documents/upload/",
            {"household_id": self.other_household.id, "file": pdf_file},
            format="multipart",
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_secure_document_download(self):
        """Member can download document; stranger gets 404."""
        pdf_content = b"%PDF-1.4\nProtected bill content\n%%EOF"
        pdf_file = SimpleUploadedFile("protected.pdf", pdf_content, content_type="application/pdf")
        doc = BillDocument.objects.create(
            household=self.household,
            uploaded_by=self.user,
            file=pdf_file,
            file_name="protected.pdf",
            file_size=len(pdf_content),
            mime_type="application/pdf",
        )

        # Member download
        res_member = self.client.get(f"/api/bills/documents/{doc.id}/download/")
        assert res_member.status_code == status.HTTP_200_OK
        assert b"%PDF-1.4" in b"".join(res_member.streaming_content)

        # Stranger download
        self.client.force_authenticate(user=self.stranger)
        res_stranger = self.client.get(f"/api/bills/documents/{doc.id}/download/")
        assert res_stranger.status_code == status.HTTP_404_NOT_FOUND
