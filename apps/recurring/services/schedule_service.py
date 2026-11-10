import logging
import calendar
from decimal import Decimal
from datetime import date, timedelta
from typing import List, Tuple, Optional, Dict, Any
from django.db import transaction
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.bills.models import Bill, BillStatus, BillCategory
from apps.recurring.models import RecurringSchedule, RecurringOccurrence, RecurrenceFrequency
from apps.reminders.services.reminder_service import ReminderService
from apps.audit.services import AuditService
from apps.audit.models import AuditEvent
from apps.accounts.models import User

logger = logging.getLogger(__name__)


def advance_calendar_months(source_date: date, months_to_add: int, target_day: int) -> date:
    """Safely adds months to a calendar date, capping days at month end."""
    month = source_date.month - 1 + months_to_add
    year = source_date.year + month // 12
    month = month % 12 + 1
    max_day_in_month = calendar.monthrange(year, month)[1]
    day = min(target_day, max_day_in_month)
    return date(year, month, day)


def calculate_next_due_date(
    current_date: date,
    frequency: str,
    interval_count: int = 1,
    target_day: int = 1,
) -> date:
    """Calculates the subsequent due date based on frequency and interval."""
    if frequency == RecurrenceFrequency.MONTHLY:
        return advance_calendar_months(current_date, interval_count, target_day)
    elif frequency == RecurrenceFrequency.QUARTERLY:
        return advance_calendar_months(current_date, 3 * interval_count, target_day)
    elif frequency == RecurrenceFrequency.YEARLY:
        year = current_date.year + interval_count
        max_day = calendar.monthrange(year, current_date.month)[1]
        day = min(target_day, max_day)
        return date(year, current_date.month, day)
    return current_date + timedelta(days=30 * interval_count)


def get_period_identifier(due_date: date, frequency: str) -> str:
    """Generates unique period token to guarantee idempotency."""
    if frequency == RecurrenceFrequency.MONTHLY:
        return due_date.strftime("%Y-%m")
    elif frequency == RecurrenceFrequency.QUARTERLY:
        quarter = (due_date.month - 1) // 3 + 1
        return f"{due_date.year}-Q{quarter}"
    elif frequency == RecurrenceFrequency.YEARLY:
        return due_date.strftime("%Y")
    return due_date.strftime("%Y-%m-%d")


class RecurringScheduleService:
    """
    Manages recurrence calculations and automatic idempotent bill generation.
    """
    @classmethod
    def create_schedule(
        cls,
        household,
        created_by,
        provider_name: str,
        category: str,
        expected_amount: Decimal,
        frequency: str = RecurrenceFrequency.MONTHLY,
        next_due_date: Optional[date] = None,
        day_of_month: int = 1,
        interval_count: int = 1,
        currency: str = "INR",
        assigned_to=None,
        is_active: bool = True,
        notes: str = "",
    ) -> RecurringSchedule:
        if not created_by or not household.is_member(created_by):
            raise ValidationError("You do not have permission to create recurring schedules for this household.")

        if not provider_name or not provider_name.strip():
            raise ValidationError("Provider name is required.")

        if not isinstance(expected_amount, Decimal):
            try:
                expected_amount = Decimal(str(expected_amount))
            except Exception:
                raise ValidationError("Expected amount must be a valid number.")

        if expected_amount <= Decimal("0.00"):
            raise ValidationError("Expected amount must be greater than zero.")

        if assigned_to:
            if not household.is_member(assigned_to):
                raise ValidationError(f"Assigned member '{assigned_to.display_name}' is not an active member of this household.")

        if not next_due_date:
            today = timezone.localdate()
            next_due_date = calculate_next_due_date(today, frequency, interval_count, day_of_month)

        schedule = RecurringSchedule.objects.create(
            household=household,
            created_by=created_by,
            assigned_to=assigned_to,
            provider_name=provider_name.strip(),
            category=category or BillCategory.OTHER,
            expected_amount=expected_amount,
            currency=currency or getattr(household, "currency", "INR"),
            frequency=frequency,
            interval_count=max(1, int(interval_count)),
            day_of_month=min(31, max(1, int(day_of_month))),
            next_due_date=next_due_date,
            is_active=is_active,
            notes=notes.strip(),
        )

        AuditService.log(
            action=AuditEvent.Action.RECURRING_CHANGED,
            actor=created_by,
            household=household,
            entity=schedule,
            description=f"Created recurring schedule '{schedule.provider_name}' ({schedule.get_frequency_display()}).",
            metadata={"expected_amount": str(schedule.expected_amount), "frequency": schedule.frequency},
        )
        return schedule

    @classmethod
    def update_schedule(
        cls,
        schedule: RecurringSchedule,
        user,
        provider_name: Optional[str] = None,
        category: Optional[str] = None,
        expected_amount: Optional[Decimal] = None,
        frequency: Optional[str] = None,
        next_due_date: Optional[date] = None,
        day_of_month: Optional[int] = None,
        interval_count: Optional[int] = None,
        currency: Optional[str] = None,
        assigned_to=None,
        is_active: Optional[bool] = None,
        notes: Optional[str] = None,
    ) -> RecurringSchedule:
        if not user or not schedule.household.is_member(user):
            raise ValidationError("You do not have permission to update this recurring schedule.")

        if provider_name is not None:
            if not provider_name.strip():
                raise ValidationError("Provider name cannot be empty.")
            schedule.provider_name = provider_name.strip()

        if category is not None:
            schedule.category = category

        if expected_amount is not None:
            if not isinstance(expected_amount, Decimal):
                expected_amount = Decimal(str(expected_amount))
            if expected_amount <= Decimal("0.00"):
                raise ValidationError("Expected amount must be greater than zero.")
            schedule.expected_amount = expected_amount

        if frequency is not None:
            schedule.frequency = frequency

        if next_due_date is not None:
            schedule.next_due_date = next_due_date

        if day_of_month is not None:
            schedule.day_of_month = min(31, max(1, int(day_of_month)))

        if interval_count is not None:
            schedule.interval_count = max(1, int(interval_count))

        if currency is not None:
            schedule.currency = currency

        if assigned_to is not None:
            if assigned_to != "" and not schedule.household.is_member(assigned_to):
                raise ValidationError("Selected user is not an active member of this household.")
            schedule.assigned_to = assigned_to if assigned_to != "" else None

        if is_active is not None:
            schedule.is_active = is_active

        if notes is not None:
            schedule.notes = notes.strip()

        schedule.save()

        AuditService.log(
            action=AuditEvent.Action.RECURRING_CHANGED,
            actor=user,
            household=schedule.household,
            entity=schedule,
            description=f"Updated recurring schedule '{schedule.provider_name}'.",
            metadata={"expected_amount": str(schedule.expected_amount), "is_active": schedule.is_active},
        )
        return schedule

    @classmethod
    def pause_schedule(cls, schedule: RecurringSchedule, user) -> RecurringSchedule:
        if not user or not schedule.household.is_member(user):
            raise ValidationError("Permission denied.")
        schedule.is_active = False
        schedule.save(update_fields=["is_active", "updated_at"])
        AuditService.log(
            action=AuditEvent.Action.RECURRING_CHANGED,
            actor=user,
            household=schedule.household,
            entity=schedule,
            description=f"Paused recurring schedule '{schedule.provider_name}'.",
        )
        return schedule

    @classmethod
    def resume_schedule(cls, schedule: RecurringSchedule, user) -> RecurringSchedule:
        if not user or not schedule.household.is_member(user):
            raise ValidationError("Permission denied.")
        schedule.is_active = True
        schedule.save(update_fields=["is_active", "updated_at"])
        AuditService.log(
            action=AuditEvent.Action.RECURRING_CHANGED,
            actor=user,
            household=schedule.household,
            entity=schedule,
            description=f"Resumed recurring schedule '{schedule.provider_name}'.",
        )
        return schedule

    @classmethod
    def trigger_occurrence(cls, schedule: RecurringSchedule, user) -> Tuple[RecurringSchedule, Bill]:
        """Manually generates the next occurrence on-demand without waiting for Celery beat."""
        if not user or not schedule.household.is_member(user):
            raise ValidationError("Permission denied.")

        period_id = get_period_identifier(schedule.next_due_date, schedule.frequency)

        with transaction.atomic():
            locked = RecurringSchedule.objects.select_for_update().get(id=schedule.id)
            if RecurringOccurrence.objects.filter(schedule=locked, period_identifier=period_id).exists():
                raise ValidationError(f"A bill occurrence for period '{period_id}' has already been generated.")

            billing_period_label = f"{locked.next_due_date.strftime('%B %Y')} ({locked.get_frequency_display()})"
            today = timezone.localdate()

            bill = Bill.objects.create(
                household=locked.household,
                uploaded_by=user,
                assigned_to=locked.assigned_to,
                provider_name=locked.provider_name,
                category=locked.category,
                amount=locked.expected_amount,
                currency=locked.currency,
                billing_period=billing_period_label,
                issue_date=today,
                due_date=locked.next_due_date,
                notes=f"Manually triggered from recurring schedule #{locked.id}.",
            )

            RecurringOccurrence.objects.create(
                schedule=locked,
                bill=bill,
                period_identifier=period_id,
            )

            ReminderService.create_reminders_for_bill(bill)

            locked.next_due_date = calculate_next_due_date(
                locked.next_due_date,
                locked.frequency,
                locked.interval_count,
                locked.day_of_month,
            )
            locked.save(update_fields=["next_due_date", "updated_at"])

            AuditService.log(
                action=AuditEvent.Action.BILL_CREATED,
                actor=user,
                household=locked.household,
                entity=bill,
                description=f"Generated bill '{bill.provider_name}' from recurring schedule #{locked.id}.",
                metadata={"amount": str(bill.amount), "due_date": str(bill.due_date)},
            )

        return locked, bill

    @classmethod
    def detect_missed_schedules(cls, household=None) -> List[Dict[str, Any]]:
        """
        Detects active recurring schedules that have due dates in the past with no occurrence generated.
        """
        today = timezone.localdate()
        qs = RecurringSchedule.objects.filter(is_active=True, next_due_date__lt=today).select_related("household", "assigned_to")
        if household:
            qs = qs.filter(household=household)

        missed = []
        for sched in qs:
            period_id = get_period_identifier(sched.next_due_date, sched.frequency)
            has_occurrence = RecurringOccurrence.objects.filter(schedule=sched, period_identifier=period_id).exists()
            if not has_occurrence:
                missed.append({
                    "schedule": sched,
                    "missed_period": period_id,
                    "missed_due_date": sched.next_due_date,
                    "days_overdue": (today - sched.next_due_date).days,
                })
        return missed

    @classmethod
    def catch_up_missed_occurrences(cls, schedule: RecurringSchedule, user, max_catch_up: int = 6) -> List[Bill]:
        """
        Generates missed occurrences for past due periods and advances schedule to the present/future.
        """
        if not user or not schedule.household.is_member(user):
            raise ValidationError("Permission denied.")

        today = timezone.localdate()
        generated = []
        iterations = 0

        while schedule.next_due_date < today and iterations < max_catch_up:
            iterations += 1
            period_id = get_period_identifier(schedule.next_due_date, schedule.frequency)

            with transaction.atomic():
                locked = RecurringSchedule.objects.select_for_update().get(id=schedule.id)
                if not RecurringOccurrence.objects.filter(schedule=locked, period_identifier=period_id).exists():
                    bill = Bill.objects.create(
                        household=locked.household,
                        uploaded_by=user,
                        assigned_to=locked.assigned_to,
                        provider_name=locked.provider_name,
                        category=locked.category,
                        amount=locked.expected_amount,
                        currency=locked.currency,
                        billing_period=f"{locked.next_due_date.strftime('%B %Y')} ({locked.get_frequency_display()})",
                        issue_date=locked.next_due_date,
                        due_date=locked.next_due_date,
                        notes=f"Catch-up occurrence for missed cycle {period_id}.",
                    )
                    RecurringOccurrence.objects.create(
                        schedule=locked,
                        bill=bill,
                        period_identifier=period_id,
                    )
                    ReminderService.create_reminders_for_bill(bill)
                    generated.append(bill)

                locked.next_due_date = calculate_next_due_date(
                    locked.next_due_date,
                    locked.frequency,
                    locked.interval_count,
                    locked.day_of_month,
                )
                locked.save(update_fields=["next_due_date", "updated_at"])
                schedule.next_due_date = locked.next_due_date

        return generated

    @classmethod
    def generate_due_occurrences(cls, generate_ahead_days: int = 15) -> List[Tuple[RecurringSchedule, Bill]]:
        """
        Finds active recurring schedules due within generate_ahead_days,
        checks occurrence existence, creates bill records idempotently,
        schedules reminders, and advances next_due_date.
        """
        today = timezone.localdate()
        cutoff_date = today + timedelta(days=generate_ahead_days)

        due_schedules = (
            RecurringSchedule.objects.filter(
                is_active=True,
                next_due_date__lte=cutoff_date,
            )
            .select_related("household", "assigned_to")
            .order_by("next_due_date")
        )

        generated_pairs = []

        for schedule in due_schedules:
            period_id = get_period_identifier(schedule.next_due_date, schedule.frequency)

            with transaction.atomic():
                # Acquire lock on schedule to prevent race conditions
                locked_schedule = RecurringSchedule.objects.select_for_update().get(id=schedule.id)

                # Idempotency check: Skip if occurrence already exists
                if RecurringOccurrence.objects.filter(schedule=locked_schedule, period_identifier=period_id).exists():
                    logger.info(
                        f"Occurrence for schedule #{locked_schedule.id} ({period_id}) already exists. Skipping duplicate."
                    )
                    # Advance date if it wasn't already advanced
                    if locked_schedule.next_due_date <= today:
                        locked_schedule.next_due_date = calculate_next_due_date(
                            locked_schedule.next_due_date,
                            locked_schedule.frequency,
                            locked_schedule.interval_count,
                            locked_schedule.day_of_month,
                        )
                        locked_schedule.save(update_fields=["next_due_date", "updated_at"])
                    continue

                # Create the confirmed Bill occurrence
                billing_period_label = (
                    f"{schedule.next_due_date.strftime('%B %Y')} "
                    f"({schedule.get_frequency_display()})"
                )
                bill = Bill.objects.create(
                    household=locked_schedule.household,
                    uploaded_by=locked_schedule.created_by,
                    assigned_to=locked_schedule.assigned_to,
                    provider_name=locked_schedule.provider_name,
                    category=locked_schedule.category,
                    amount=locked_schedule.expected_amount,
                    currency=locked_schedule.currency,
                    billing_period=billing_period_label,
                    issue_date=today,
                    due_date=locked_schedule.next_due_date,
                    notes=f"Auto-generated from Recurring Schedule #{locked_schedule.id}.",
                )

                # Record occurrence to prevent duplicates
                RecurringOccurrence.objects.create(
                    schedule=locked_schedule,
                    bill=bill,
                    period_identifier=period_id,
                )

                # Automatically schedule milestone reminders
                ReminderService.create_reminders_for_bill(bill)

                # Advance next_due_date to next cycle
                locked_schedule.next_due_date = calculate_next_due_date(
                    locked_schedule.next_due_date,
                    locked_schedule.frequency,
                    locked_schedule.interval_count,
                    locked_schedule.day_of_month,
                )
                locked_schedule.save(update_fields=["next_due_date", "updated_at"])

                generated_pairs.append((locked_schedule, bill))
                logger.info(
                    f"Generated recurring Bill #{bill.id} for schedule #{locked_schedule.id} "
                    f"({locked_schedule.provider_name}). Next due: {locked_schedule.next_due_date}."
                )

        return generated_pairs
