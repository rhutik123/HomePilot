from decimal import Decimal
import pytest
from django.contrib.auth import get_user_model
from django.contrib.admin.sites import site

from apps.households.models import Household, HouseholdMembership
from apps.bills.models import Bill, BillStatus
from apps.audit.models import AuditEvent
from apps.audit.services import AuditService, sanitize_metadata

User = get_user_model()


@pytest.mark.django_db
class TestAuditDomain:
    def setup_method(self):
        self.user = User.objects.create_user(
            username="auditor",
            email="auditor@example.com",
            password="SecurePassword123!",
        )
        self.household = Household.objects.create(name="Audit Test Home", created_by=self.user)
        HouseholdMembership.objects.create(
            household=self.household,
            user=self.user,
            role=HouseholdMembership.Role.OWNER,
        )

    def test_sanitize_metadata_recursively_masks_sensitive_keys(self):
        """Verify sensitive credentials and tokens are redacted before storage."""
        raw_metadata = {
            "password": "supersecretpassword",
            "api_key": "AIzaSyDummyKey",
            "token": "bearer-token-123",
            "secret": "topsecret",
            "csrfmiddlewaretoken": "csrf123456",
            "nested": {
                "PASSWORD": "hiddenpassword",
                "normal_field": "safe_value",
                "nested_array": [
                    {"api_key": "array_key", "public": 42},
                    "string_value",
                ],
            },
            "clean_field": "visible",
        }

        sanitized = sanitize_metadata(raw_metadata)

        assert sanitized["password"] == "******"
        assert sanitized["api_key"] == "******"
        assert sanitized["token"] == "******"
        assert sanitized["secret"] == "******"
        assert sanitized["csrfmiddlewaretoken"] == "******"
        assert sanitized["nested"]["PASSWORD"] == "******"
        assert sanitized["nested"]["normal_field"] == "safe_value"
        assert sanitized["nested"]["nested_array"][0]["api_key"] == "******"
        assert sanitized["nested"]["nested_array"][0]["public"] == 42
        assert sanitized["clean_field"] == "visible"

    def test_audit_service_log_creates_event(self):
        """Verify AuditService.log creates a valid AuditEvent record with sanitized metadata."""
        bill = Bill.objects.create(
            household=self.household,
            uploaded_by=self.user,
            assigned_to=self.user,
            provider_name="Electric Utility",
            amount=Decimal("1500.00"),
            due_date="2026-12-01",
            status=BillStatus.PENDING,
        )

        event = AuditService.log(
            action=AuditEvent.Action.BILL_CREATED,
            actor=self.user,
            household=self.household,
            entity=bill,
            description="Bill created for Electric Utility",
            metadata={"amount": "1500.00", "password": "sensitive_data"},
            ip_address="127.0.0.1",
        )

        assert event is not None
        assert event.actor == self.user
        assert event.household == self.household
        assert event.action == AuditEvent.Action.BILL_CREATED
        assert event.entity_type == "Bill"
        assert event.entity_id == bill.id
        assert event.description == "Bill created for Electric Utility"
        assert event.metadata["password"] == "******"
        assert event.metadata["amount"] == "1500.00"
        assert event.ip_address == "127.0.0.1"

    def test_audit_service_auto_infers_household_from_entity(self):
        """When household is not explicitly provided, AuditService infers it from entity.household."""
        bill = Bill.objects.create(
            household=self.household,
            uploaded_by=self.user,
            assigned_to=self.user,
            provider_name="Gas Co",
            amount=Decimal("600.00"),
            due_date="2026-12-01",
        )

        event = AuditService.log(
            action=AuditEvent.Action.BILL_UPDATED,
            actor=self.user,
            entity=bill,
            description="Updated gas bill",
        )

        assert event is not None
        assert event.household == self.household

    def test_audit_event_string_representation(self):
        """Verify string formatting of AuditEvent."""
        event = AuditEvent.objects.create(
            actor=self.user,
            household=self.household,
            action=AuditEvent.Action.BILL_PAID,
            description="Marked as paid",
        )
        assert "BILL_PAID" in str(event)
        assert "auditor" in str(event)

    def test_audit_event_registered_in_admin(self):
        """Verify AuditEvent is registered in the Django admin site."""
        assert AuditEvent in site._registry
