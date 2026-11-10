import logging
import calendar
from datetime import date, timedelta
from typing import List, Tuple
from django.db import transaction
from django.utils import timezone

from apps.bills.models import Bill, BillStatus
from apps.recurring.models import RecurringSchedule, RecurringOccurrence, RecurrenceFrequency
from apps.reminders.services.reminder_service import ReminderService

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
