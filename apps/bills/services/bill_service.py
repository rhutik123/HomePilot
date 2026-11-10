import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Optional, Any, Dict
from django.db import transaction
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.bills.models import Bill, BillDocument, BillCategory, BillStatus
from apps.households.models import Household
from apps.reminders.services.reminder_service import ReminderService
from apps.audit.services import AuditService
from apps.audit.models import AuditEvent

logger = logging.getLogger(__name__)


def parse_date(value: Any) -> Optional[date]:
    """Helper to parse a date object or string into a datetime.date."""
    if not value:
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
        return datetime.strptime(value, "%Y-%m-%d").date()
    return None


class BillService:
    """
    Central business service for bill lifecycle operations.
    Unifies bill creation (upload-based and manual) and updates across both
    the Web UI and the REST API to guarantee invariant validation, tenant
    isolation, reminder scheduling, and audit trail generation.
    """

    @classmethod
    def create_bill(
        cls,
        household: Household,
        uploaded_by,
        provider_name: str,
        amount: Any,
        due_date: Any,
        category: str = BillCategory.OTHER,
        currency: str = "INR",
        assigned_to=None,
        issue_date: Any = None,
        billing_period: str = "",
        reference_number: str = "",
        notes: str = "",
        document: Optional[BillDocument] = None,
        source: str = "MANUAL",
    ) -> Bill:
        """
        Atomically creates a new Bill directly in the database.
        Works identically for manual entry and document-attached bills.
        Never creates a fake or placeholder document for manual entry.
        """
        # 1. Tenant Authorization
        if not uploaded_by or not uploaded_by.is_authenticated:
            raise ValidationError("Authentication required to create a bill.")

        if not household or not household.is_member(uploaded_by):
            raise ValidationError("You do not have permission to create bills for this household.")

        # 2. Member Assignee Validation
        if assigned_to is not None:
            if not household.is_member(assigned_to):
                display = getattr(assigned_to, "display_name", str(assigned_to))
                raise ValidationError(f"Assigned member '{display}' is not an active member of '{household.name}'.")

        # 3. Required Fields & Value Validations
        if not provider_name or not str(provider_name).strip():
            raise ValidationError("Provider or company name is required.")

        try:
            amount_decimal = Decimal(str(amount))
        except Exception:
            raise ValidationError("Bill amount must be a valid decimal number.")

        if amount_decimal <= Decimal("0.00"):
            raise ValidationError("Bill amount must be greater than zero.")

        parsed_due_date = parse_date(due_date)
        if not parsed_due_date:
            raise ValidationError("A valid payment due date is required.")

        parsed_issue_date = parse_date(issue_date)

        if category not in BillCategory.values:
            category = BillCategory.OTHER

        currency_code = (currency or "INR").strip().upper()

        # 4. Document Verification (if provided)
        if document is not None:
            if document.household_id != household.id:
                raise ValidationError("Document does not belong to the selected household.")

        # 5. Atomic Persistence
        with transaction.atomic():
            bill = Bill.objects.create(
                household=household,
                uploaded_by=uploaded_by,
                assigned_to=assigned_to,
                provider_name=provider_name.strip(),
                category=category,
                amount=amount_decimal,
                currency=currency_code,
                due_date=parsed_due_date,
                issue_date=parsed_issue_date,
                billing_period=(billing_period or "").strip(),
                reference_number=(reference_number or "").strip(),
                notes=(notes or "").strip(),
                status=BillStatus.PENDING,
                outstanding_balance=amount_decimal,
                total_paid=Decimal("0.00"),
            )

            # Link existing uploaded document if present
            if document is not None:
                document.bill = bill
                document.save(update_fields=["bill"])

            # Schedule milestone reminders
            ReminderService.create_reminders_for_bill(bill)

            # Record compliance audit trail
            AuditService.log(
                action=AuditEvent.Action.BILL_CREATED,
                actor=uploaded_by,
                household=household,
                entity=bill,
                description=f"Bill '{bill.provider_name}' (₹{bill.amount}) created via {source.lower().replace('_', ' ')}.",
                metadata={
                    "amount": str(bill.amount),
                    "currency": bill.currency,
                    "category": bill.category,
                    "due_date": bill.due_date.isoformat(),
                    "source": source,
                    "has_document": document is not None,
                    "assigned_to_id": assigned_to.id if assigned_to else None,
                },
            )

            logger.info("Bill #%s created for household '%s' via %s", bill.id, household.name, source)
            return bill

    @classmethod
    def update_bill(
        cls,
        bill: Bill,
        user,
        **fields: Any,
    ) -> Bill:
        """
        Safely updates an existing Bill while preserving immutable fields
        (e.g. household, status) and synchronizing reminder schedules.
        """
        if not user or not user.is_authenticated or not bill.household.is_member(user):
            raise ValidationError("You do not have permission to modify this bill.")

        # Disallow tampering with immutable fields through update
        if "household" in fields and fields["household"] != bill.household:
            raise ValidationError("Household cannot be changed once a bill is created.")
        fields.pop("household", None)
        fields.pop("status", None)
        fields.pop("total_paid", None)
        fields.pop("outstanding_balance", None)

        # Validate assignee if modified
        if "assigned_to" in fields:
            new_assignee = fields["assigned_to"]
            if new_assignee is not None and not bill.household.is_member(new_assignee):
                display = getattr(new_assignee, "display_name", str(new_assignee))
                raise ValidationError(f"Assigned member '{display}' is not an active member of '{bill.household.name}'.")

        # Validate amount if modified
        if "amount" in fields:
            try:
                amt = Decimal(str(fields["amount"]))
                if amt <= Decimal("0.00"):
                    raise ValueError
                fields["amount"] = amt
            except Exception:
                raise ValidationError("Bill amount must be a positive decimal number.")

        # Validate dates if modified
        if "due_date" in fields:
            d_date = parse_date(fields["due_date"])
            if not d_date:
                raise ValidationError("A valid payment due date is required.")
            fields["due_date"] = d_date

        if "issue_date" in fields:
            fields["issue_date"] = parse_date(fields["issue_date"])

        if "category" in fields and fields["category"] not in BillCategory.values:
            fields["category"] = BillCategory.OTHER

        with transaction.atomic():
            old_due_date = bill.due_date
            old_assigned_to = bill.assigned_to
            old_amount = bill.amount

            for attr, val in fields.items():
                setattr(bill, attr, val)

            bill.save()

            if "amount" in fields and bill.amount != old_amount:
                bill.recalculate_balance()

            # Reschedule milestone reminders if due_date or assigned_to changed
            if bill.due_date != old_due_date or bill.assigned_to != old_assigned_to:
                ReminderService.reschedule_reminders_for_bill(bill)

            # Audit event
            AuditService.log(
                action=AuditEvent.Action.BILL_UPDATED,
                actor=user,
                household=bill.household,
                entity=bill,
                description=f"Bill '{bill.provider_name}' updated.",
                metadata={
                    "updated_fields": list(fields.keys()),
                    "amount": str(bill.amount),
                    "due_date": bill.due_date.isoformat(),
                },
            )

            logger.info("Bill #%s updated by user %s", bill.id, user.username)
            return bill
