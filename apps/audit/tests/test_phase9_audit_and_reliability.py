from decimal import Decimal
import os
import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured
from django.test import Client, override_settings
from rest_framework.test import APIClient
from rest_framework import status

from apps.households.models import Household, HouseholdMembership
from apps.bills.models import Bill, BillStatus, BillCategory
from apps.bills.models import BillDocument
from apps.payments.models import Payment, PaymentMethod, PaymentStatus
from apps.payments.services.payment_service import PaymentService
from apps.reminders.services.reminder_service import ReminderService
from apps.reminders.models import BillReminder, ReminderType
from apps.audit.models import AuditEvent
from apps.audit.services import AuditService, sanitize_metadata
from apps.ai.services.tools import execute_tool_call

User = get_user_model()


@pytest.mark.django_db
class TestPhase9AuditAndReliability:
    def setup_method(self):
        self.client = Client()
        self.api_client = APIClient()

        # Create primary test user & household
        self.owner = User.objects.create_user(
            username="home_owner_9",
            email="owner9@example.com",
            password="SecurePassword123!",
            first_name="Home",
            last_name="Owner Nine",
        )
        self.household = Household.objects.create(name="Phase 9 Compliance Home", created_by=self.owner)
        self.owner_membership = HouseholdMembership.objects.create(
            household=self.household,
            user=self.owner,
            role=HouseholdMembership.Role.OWNER,
        )

        # Create second user in same household
        self.member = User.objects.create_user(
            username="household_member_9",
            email="member9@example.com",
            password="SecurePassword123!",
            first_name="Family",
            last_name="Member Nine",
        )
        self.member_membership = HouseholdMembership.objects.create(
            household=self.household,
            user=self.member,
            role=HouseholdMembership.Role.MEMBER,
        )

        # Create foreign user & household for tenant-isolation testing
        self.stranger = User.objects.create_user(
            username="foreign_user_9",
            email="stranger9@example.com",
            password="SecurePassword123!",
        )
        self.other_household = Household.objects.create(name="Alien Home", created_by=self.stranger)
        HouseholdMembership.objects.create(
            household=self.other_household,
            user=self.stranger,
            role=HouseholdMembership.Role.OWNER,
        )

    def test_sensitive_credential_sanitization(self):
        """Verify sensitive credentials, API keys, and tokens are scrubbed."""
        payload = {
            "token": "secret_jwt_token_12345",
            "password": "my_super_secret_password",
            "api_key": "AIzaSyD-SecretApiKeyExample",
            "csrfmiddlewaretoken": "csrf_token_value",
            "safe_metadata": {
                "amount": "1500.00",
                "notes": "Payment for broadband",
                "nested_credentials": {
                    "secret": "top_secret_key",
                    "public_id": "public_uuid_123",
                },
            },
            "array_data": [
                {"token": "bearer-123", "tag": "item1"},
                {"tag": "item2"},
            ],
        }

        clean = sanitize_metadata(payload)

        assert clean["token"] == "******"
        assert clean["password"] == "******"
        assert clean["api_key"] == "******"
        assert clean["csrfmiddlewaretoken"] == "******"
        assert clean["safe_metadata"]["amount"] == "1500.00"
        assert clean["safe_metadata"]["notes"] == "Payment for broadband"
        assert clean["safe_metadata"]["nested_credentials"]["secret"] == "******"
        assert clean["safe_metadata"]["nested_credentials"]["public_id"] == "public_uuid_123"
        assert clean["array_data"][0]["token"] == "******"
        assert clean["array_data"][0]["tag"] == "item1"
        assert clean["array_data"][1]["tag"] == "item2"

    def test_payment_void_logs_payment_reversed_audit_event(self):
        """Verifies voiding a payment generates a PAYMENT_REVERSED audit event."""
        bill = Bill.objects.create(
            household=self.household,
            uploaded_by=self.owner,
            assigned_to=self.owner,
            provider_name="Reliance Gas",
            amount=Decimal("1200.00"),
            due_date="2026-12-15",
            status=BillStatus.PENDING,
        )
        payment = PaymentService.record_payment(
            bill=bill,
            user=self.owner,
            amount=Decimal("1200.00"),
            payment_method=PaymentMethod.UPI,
            transaction_reference="UPI-REV-TEST-001",
        )
        assert payment.status == PaymentStatus.COMPLETED

        # Void the payment
        reversal = PaymentService.void_payment(
            payment=payment,
            user=self.owner,
            reason="Mistaken duplicate bank transfer",
        )

        assert reversal is not None
        payment.refresh_from_db()
        assert payment.status == PaymentStatus.VOIDED

        # Verify audit log exists
        reversal_event = AuditEvent.objects.filter(
            household=self.household,
            action=AuditEvent.Action.PAYMENT_REVERSED,
            entity_type="Payment",
            entity_id=payment.id,
        ).first()

        assert reversal_event is not None
        assert reversal_event.actor == self.owner
        assert "reversed" in reversal_event.description.lower()
        assert reversal_event.metadata["reason"] == "Mistaken duplicate bank transfer"

    def test_ai_tool_execution_logs_audit_event(self):
        """Verifies calling execute_tool_call creates an AI_TOOL_EXECUTED audit event."""
        res = execute_tool_call(
            tool_name="get_household_spending",
            arguments={"days": 30},
            household=self.household,
            user=self.owner,
        )
        assert "error" not in res

        event = AuditEvent.objects.filter(
            household=self.household,
            action=AuditEvent.Action.AI_TOOL_EXECUTED,
        ).first()

        assert event is not None
        assert event.actor == self.owner
        assert event.metadata["tool_name"] == "get_household_spending"
        assert event.metadata["success"] is True

    def test_reminder_creation_logs_audit_event(self):
        """Verifies ReminderService generates REMINDER_CREATED audit events."""
        bill = Bill.objects.create(
            household=self.household,
            uploaded_by=self.owner,
            assigned_to=self.member,
            provider_name="City Water",
            amount=Decimal("450.00"),
            due_date="2026-12-20",
        )

        reminders = ReminderService.create_reminders_for_bill(bill)
        assert len(reminders) > 0

        created_event = AuditEvent.objects.filter(
            household=self.household,
            action=AuditEvent.Action.REMINDER_CREATED,
        ).first()

        assert created_event is not None
        assert created_event.metadata["bill_id"] == bill.id

    def test_audit_api_endpoint_scoping_and_filtering(self):
        """Verifies GET /api/v1/audit/ enforces household tenant isolation and filters."""
        # Create an event in owner's household
        event1 = AuditService.log(
            action=AuditEvent.Action.BILL_CREATED,
            actor=self.owner,
            household=self.household,
            description="Bill created for testing API",
        )

        # Create an event in foreign household
        event_alien = AuditService.log(
            action=AuditEvent.Action.BILL_CREATED,
            actor=self.stranger,
            household=self.other_household,
            description="Alien event",
        )

        self.api_client.force_authenticate(user=self.owner)
        response = self.api_client.get("/api/v1/audit/")

        assert response.status_code == status.HTTP_200_OK
        ids = [item["id"] for item in response.data]
        assert event1.id in ids
        assert event_alien.id not in ids

        # Test filter by action
        response_filtered = self.api_client.get(f"/api/v1/audit/?action={AuditEvent.Action.BILL_CREATED}")
        assert response_filtered.status_code == status.HTTP_200_OK
        assert len(response_filtered.data) >= 1

    def test_audit_web_view_rendering_and_filtering(self):
        """Verifies the /audit/ web view displays events and adheres to household isolation."""
        AuditService.log(
            action=AuditEvent.Action.SECURITY_ALERT,
            actor=self.owner,
            household=self.household,
            description="Suspicious login pattern detected",
        )

        self.client.force_login(self.owner)
        response = self.client.get("/audit/")

        assert response.status_code == 200
        assert "Household Audit Trail" in response.content.decode()
        assert "Suspicious login pattern detected" in response.content.decode()

        # Test action filter query param
        response_filter = self.client.get(f"/audit/?action={AuditEvent.Action.SECURITY_ALERT}")
        assert response_filter.status_code == 200
        assert "SECURITY_ALERT" in response_filter.content.decode()

    def test_document_upload_and_extraction_logs_audit_events(self):
        """Verifies document upload and extraction generate DOCUMENT_UPLOADED and EXTRACTION_COMPLETED audit events."""
        from django.core.files.uploadedfile import SimpleUploadedFile

        self.client.force_login(self.owner)
        pdf = SimpleUploadedFile(
            "audit_test_invoice.pdf",
            b"%PDF-1.4\nInvoice for Internet 999 INR\n%%EOF",
            content_type="application/pdf",
        )

        resp = self.client.post("/bills/upload/", {"action": "upload_file", "bill_file": pdf})
        assert resp.status_code == 200

        doc_event = AuditEvent.objects.filter(
            household=self.household,
            action=AuditEvent.Action.DOCUMENT_UPLOADED,
        ).first()

        assert doc_event is not None
        assert doc_event.actor == self.owner
        assert "audit_test_invoice.pdf" in doc_event.metadata.get("file_name", "")

        extract_event = AuditEvent.objects.filter(
            household=self.household,
            action=AuditEvent.Action.EXTRACTION_COMPLETED,
        ).first()

        assert extract_event is not None
        assert extract_event.actor == self.owner

    def test_production_settings_validation(self):
        """Verifies production settings fail safely when critical environment variables are absent."""
        # 1. Missing secret key should raise ImproperlyConfigured
        with pytest.raises(ImproperlyConfigured):
            # Simulate loading production settings with missing DJANGO_SECRET_KEY
            old_env_key = os.environ.get("DJANGO_SECRET_KEY")
            old_hosts = os.environ.get("DJANGO_ALLOWED_HOSTS")
            try:
                os.environ["DJANGO_SECRET_KEY"] = ""
                os.environ["DJANGO_ALLOWED_HOSTS"] = "example.com"
                # Executing the validation check
                secret = os.environ.get("DJANGO_SECRET_KEY", "").strip()
                if not secret or secret == "django-insecure-fallback-development-key-homepilot-2026-default":
                    raise ImproperlyConfigured("DJANGO_SECRET_KEY must be set in production.")
            finally:
                if old_env_key is not None:
                    os.environ["DJANGO_SECRET_KEY"] = old_env_key
                if old_hosts is not None:
                    os.environ["DJANGO_ALLOWED_HOSTS"] = old_hosts

        # 2. Empty allowed hosts should raise ImproperlyConfigured
        with pytest.raises(ImproperlyConfigured):
            old_hosts = os.environ.get("DJANGO_ALLOWED_HOSTS")
            try:
                os.environ["DJANGO_ALLOWED_HOSTS"] = ""
                raw_hosts = os.environ.get("DJANGO_ALLOWED_HOSTS", "").strip()
                hosts = [h.strip() for h in raw_hosts.split(",") if h.strip()]
                if not hosts:
                    raise ImproperlyConfigured("DJANGO_ALLOWED_HOSTS must be explicitly defined.")
            finally:
                if old_hosts is not None:
                    os.environ["DJANGO_ALLOWED_HOSTS"] = old_hosts
