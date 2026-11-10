from datetime import date, timedelta
from decimal import Decimal
import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.households.models import Household, HouseholdMembership
from apps.bills.models import Bill, BillDocument, BillStatus, BillCategory
from apps.bills.services.bill_service import BillService
from apps.audit.models import AuditEvent
from apps.reminders.models import BillReminder

User = get_user_model()


@pytest.mark.django_db
class TestPhase1BillService:
    def setup_method(self):
        self.owner = User.objects.create_user(
            username="alice_p1",
            email="alice_p1@example.com",
            password="SecurePassword123!",
            first_name="Alice",
            last_name="Owner",
        )
        self.member = User.objects.create_user(
            username="bob_p1",
            email="bob_p1@example.com",
            password="SecurePassword123!",
            first_name="Bob",
            last_name="Member",
        )
        self.outsider = User.objects.create_user(
            username="charlie_p1",
            email="charlie_p1@example.com",
            password="SecurePassword123!",
        )

        self.household = Household.objects.create(name="Greenwood Villa", created_by=self.owner)
        HouseholdMembership.objects.create(household=self.household, user=self.owner, role=HouseholdMembership.Role.OWNER)
        HouseholdMembership.objects.create(household=self.household, user=self.member, role=HouseholdMembership.Role.MEMBER)

    def test_create_bill_manual_success(self):
        """Option B: Creates a bill manually without a digital document or attachment."""
        today = timezone.localdate()
        due_date = today + timedelta(days=15)

        bill = BillService.create_bill(
            household=self.household,
            uploaded_by=self.owner,
            provider_name="Municipal Water Board",
            amount="750.50",
            due_date=due_date,
            category=BillCategory.WATER,
            currency="INR",
            assigned_to=self.member,
            notes="Physical receipt collected at society office.",
            source="MANUAL",
        )

        assert bill.id is not None
        assert bill.provider_name == "Municipal Water Board"
        assert bill.amount == Decimal("750.50")
        assert bill.outstanding_balance == Decimal("750.50")
        assert bill.status == BillStatus.PENDING
        assert bill.assigned_to == self.member
        # Crucial requirement: No fake or placeholder document created
        assert bill.documents.count() == 0

        # Automatic milestone reminders created
        reminders = BillReminder.objects.filter(bill=bill)
        assert reminders.count() > 0
        assert all(r.sent_to == self.member for r in reminders)

        # Audit event created with source="MANUAL"
        audit = AuditEvent.objects.filter(household=self.household, entity_id=bill.id, action=AuditEvent.Action.BILL_CREATED).first()
        assert audit is not None
        assert audit.metadata.get("source") == "MANUAL"
        assert audit.metadata.get("has_document") is False

    def test_create_bill_with_document_success(self):
        """Option A: Creates a bill from document review and links the document."""
        doc = BillDocument.objects.create(
            household=self.household,
            uploaded_by=self.owner,
            file_name="electricity_oct2026.pdf",
            file_size=2048,
            mime_type="application/pdf",
        )

        today = timezone.localdate()
        due_date = today + timedelta(days=7)

        bill = BillService.create_bill(
            household=self.household,
            uploaded_by=self.owner,
            provider_name="Tata Power",
            amount=Decimal("1850.00"),
            due_date=due_date,
            category=BillCategory.ELECTRICITY,
            document=doc,
            source="DOCUMENT_UPLOAD",
        )

        assert bill.id is not None
        doc.refresh_from_db()
        assert doc.bill == bill
        assert bill.documents.filter(id=doc.id).exists()

        audit = AuditEvent.objects.filter(household=self.household, entity_id=bill.id, action=AuditEvent.Action.BILL_CREATED).first()
        assert audit is not None
        assert audit.metadata.get("source") == "DOCUMENT_UPLOAD"
        assert audit.metadata.get("has_document") is True

    def test_create_bill_rejects_unauthorized_user(self):
        """Users who are not members of the household cannot create bills."""
        today = timezone.localdate()
        with pytest.raises(ValidationError) as exc:
            BillService.create_bill(
                household=self.household,
                uploaded_by=self.outsider,
                provider_name="Intruder Utility",
                amount="100.00",
                due_date=today + timedelta(days=5),
            )
        assert "do not have permission" in str(exc.value)

    def test_create_bill_rejects_outsider_assignee(self):
        """Cannot assign a bill to a user who is not a member of the household."""
        today = timezone.localdate()
        with pytest.raises(ValidationError) as exc:
            BillService.create_bill(
                household=self.household,
                uploaded_by=self.owner,
                provider_name="Security Fee",
                amount="500.00",
                due_date=today + timedelta(days=5),
                assigned_to=self.outsider,
            )
        assert "not an active member" in str(exc.value)

    def test_create_bill_rejects_invalid_amount(self):
        """Amount must be positive decimal number."""
        today = timezone.localdate()
        for bad_amount in ["-50.00", "0.00", "abc"]:
            with pytest.raises(ValidationError):
                BillService.create_bill(
                    household=self.household,
                    uploaded_by=self.owner,
                    provider_name="Test Bad Amount",
                    amount=bad_amount,
                    due_date=today + timedelta(days=5),
                )

    def test_update_bill_success(self):
        """Updating bill due date, assignee, and amount recalculates balance and reschedules reminders."""
        today = timezone.localdate()
        bill = BillService.create_bill(
            household=self.household,
            uploaded_by=self.owner,
            provider_name="Internet Fiber",
            amount=Decimal("1000.00"),
            due_date=today + timedelta(days=5),
            assigned_to=self.owner,
        )

        new_due_date = today + timedelta(days=12)
        updated = BillService.update_bill(
            bill=bill,
            user=self.owner,
            amount="1200.00",
            due_date=new_due_date,
            assigned_to=self.member,
            notes="Upgraded plan to 300Mbps",
        )

        assert updated.amount == Decimal("1200.00")
        assert updated.outstanding_balance == Decimal("1200.00")
        assert updated.due_date == new_due_date
        assert updated.assigned_to == self.member

        # Reminders updated with new recipient and milestone dates
        reminders = BillReminder.objects.filter(bill=bill)
        assert all(r.sent_to == self.member for r in reminders)

        # Audit event recorded
        audit = AuditEvent.objects.filter(household=self.household, entity_id=bill.id, action=AuditEvent.Action.BILL_UPDATED).first()
        assert audit is not None
        assert "amount" in audit.metadata.get("updated_fields", [])
