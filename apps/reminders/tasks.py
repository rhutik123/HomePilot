import logging
from celery import shared_task
from django.db import transaction
from django.utils import timezone

from apps.bills.models import Bill, BillStatus
from apps.reminders.models import BillReminder, ReminderType
from apps.reminders.services.email_service import EmailNotificationService

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def send_bill_reminder_task(self, reminder_id: int):
    """
    Celery background worker task to dispatch an individual bill reminder.
    Implements database-level row locking (select_for_update) to prevent race conditions
    and guarantee strictly idempotent delivery.
    """
    try:
        with transaction.atomic():
            try:
                reminder = BillReminder.objects.select_for_update().select_related("bill", "sent_to", "bill__household").get(
                    id=reminder_id
                )
            except BillReminder.DoesNotExist:
                logger.warning(f"Reminder {reminder_id} not found.")
                return False

            # Idempotency check: Skip if already dispatched
            if reminder.is_sent:
                logger.info(f"Reminder {reminder_id} has already been sent. Skipping duplicate.")
                return True

            # If bill was already marked paid, cancel this reminder
            if reminder.bill.status == BillStatus.PAID:
                logger.info(f"Bill {reminder.bill_id} is already paid. Skipping reminder {reminder_id}.")
                reminder.delete()
                return False

            # Dispatch notification
            EmailNotificationService.send_bill_reminder(reminder)

            # Mark reminder as completed
            reminder.is_sent = True
            reminder.sent_at = timezone.now()
            reminder.save(update_fields=["is_sent", "sent_at"])

            logger.info(f"Successfully dispatched reminder {reminder_id} to {reminder.sent_to.email}.")
            return True

    except Exception as exc:
        logger.error(f"Error executing reminder task {reminder_id}: {exc}")
        # Exponential backoff retry (e.g. 60s, 120s, 240s)
        raise self.retry(exc=exc, countdown=60 * (2 ** self.request.retries))


@shared_task
def process_daily_reminders():
    """
    Periodic task run daily by Celery Beat (e.g. 08:00 AM).
    Finds all active reminders due on or before today and triggers delivery.
    """
    today = timezone.localdate()
    pending_reminders = BillReminder.objects.filter(
        is_sent=False,
        remind_at__lte=today,
        bill__status=BillStatus.PENDING,
    ).values_list("id", flat=True)

    dispatched_count = 0
    for reminder_id in pending_reminders:
        send_bill_reminder_task.delay(reminder_id)
        dispatched_count += 1

    logger.info(f"Periodic check complete: Dispatched {dispatched_count} reminders for date {today}.")
    return dispatched_count


@shared_task
def check_and_update_overdue_bills():
    """
    Periodic task run daily (e.g. 00:05 AM) to detect overdue bills,
    update their status, and send overdue alerts.
    """
    today = timezone.localdate()
    overdue_bills = Bill.objects.filter(
        due_date__lt=today,
        status=BillStatus.PENDING,
    )

    updated_count = 0
    for bill in overdue_bills:
        bill.status = BillStatus.OVERDUE
        bill.save(update_fields=["status", "updated_at"])

        # Create an OVERDUE alert reminder if not already existing
        recipient = bill.assigned_to or bill.household.created_by
        reminder, created = BillReminder.objects.get_or_create(
            bill=bill,
            reminder_type=ReminderType.OVERDUE,
            defaults={
                "remind_at": today,
                "sent_to": recipient,
            },
        )
        if created:
            send_bill_reminder_task.delay(reminder.id)

        updated_count += 1

    logger.info(f"Overdue check complete: Updated {updated_count} bills to OVERDUE.")
    return updated_count
