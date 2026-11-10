from datetime import timedelta
from django.utils import timezone
from apps.reminders.models import BillReminder, ReminderType, NotificationPreference
from apps.audit.models import AuditEvent
from apps.audit.services import AuditService


class ReminderService:
    """
    Manages the creation, scheduling, lifecycle, and safe cancellation of bill reminders.
    Preserves historical reminders for auditability without hard-deleting database records.
    """
    @classmethod
    def create_reminders_for_bill(cls, bill):
        """
        Generates payment reminders for a confirmed bill according to milestone rules:
        - 7 days prior to due date (if enabled)
        - 3 days prior to due date (if enabled)
        - 1 day prior to due date (if enabled)
        - On the payment due date itself (if enabled)
        Only creates reminders for dates that have not already elapsed.
        Idempotent: skips milestones already created.
        """
        today = timezone.localdate()
        due_date = bill.due_date

        if not due_date:
            return []

        if isinstance(due_date, str):
            from datetime import date
            due_date = date.fromisoformat(due_date)

        # Target recipient: assigned family member, or household owner
        recipient = bill.assigned_to or bill.household.created_by

        # Retrieve user notification preferences if configured
        prefs = None
        if recipient:
            prefs = NotificationPreference.objects.filter(user=recipient).first()

        milestones = []
        if not prefs or prefs.remind_7_days_before:
            milestones.append((due_date - timedelta(days=7), ReminderType.SEVEN_DAYS_BEFORE))
        if not prefs or prefs.remind_3_days_before:
            milestones.append((due_date - timedelta(days=3), ReminderType.THREE_DAYS_BEFORE))
        if not prefs or prefs.remind_1_day_before:
            milestones.append((due_date - timedelta(days=1), ReminderType.ONE_DAY_BEFORE))
        if not prefs or prefs.remind_on_due_date:
            milestones.append((due_date, ReminderType.DUE_DATE))

        created_reminders = []
        for remind_date, reminder_type in milestones:
            # Only schedule future or current reminders
            if remind_date >= today:
                reminder, created = BillReminder.objects.get_or_create(
                    bill=bill,
                    reminder_type=reminder_type,
                    defaults={
                        "remind_at": remind_date,
                        "sent_to": recipient,
                        "status": BillReminder.ReminderStatus.PENDING,
                        "is_sent": False,
                    },
                )
                if created:
                    AuditService.log(
                        action=AuditEvent.Action.REMINDER_CREATED,
                        household=bill.household,
                        entity=reminder,
                        description=f"Scheduled reminder ({reminder_type}) for Bill #{bill.id} on {remind_date}.",
                        metadata={"bill_id": bill.id, "reminder_type": reminder_type, "remind_at": str(remind_date)},
                    )
                created_reminders.append(reminder)

        return created_reminders

    @classmethod
    def reschedule_reminders_for_bill(cls, bill):
        """
        Recalculates milestone remind_at dates and recipient for all active,
        unsent reminders when a bill's due_date or assigned_to changes.
        """
        today = timezone.localdate()
        due_date = bill.due_date
        if not due_date:
            return []

        if isinstance(due_date, str):
            from datetime import date
            due_date = date.fromisoformat(due_date)

        recipient = bill.assigned_to or bill.household.created_by
        prefs = None
        if recipient:
            prefs = NotificationPreference.objects.filter(user=recipient).first()

        milestones = []
        if not prefs or prefs.remind_7_days_before:
            milestones.append((due_date - timedelta(days=7), ReminderType.SEVEN_DAYS_BEFORE))
        if not prefs or prefs.remind_3_days_before:
            milestones.append((due_date - timedelta(days=3), ReminderType.THREE_DAYS_BEFORE))
        if not prefs or prefs.remind_1_day_before:
            milestones.append((due_date - timedelta(days=1), ReminderType.ONE_DAY_BEFORE))
        if not prefs or prefs.remind_on_due_date:
            milestones.append((due_date, ReminderType.DUE_DATE))

        updated_reminders = []
        for remind_date, reminder_type in milestones:
            reminder = BillReminder.objects.filter(bill=bill, reminder_type=reminder_type).first()
            if reminder:
                if not reminder.is_sent and reminder.status != BillReminder.ReminderStatus.CANCELLED:
                    reminder.remind_at = remind_date
                    reminder.sent_to = recipient
                    reminder.status = BillReminder.ReminderStatus.PENDING
                    reminder.save(update_fields=["remind_at", "sent_to", "status"])
                    updated_reminders.append(reminder)
            else:
                # If milestone is future/today and does not yet exist, create it
                if remind_date >= today:
                    new_reminder = BillReminder.objects.create(
                        bill=bill,
                        reminder_type=reminder_type,
                        remind_at=remind_date,
                        sent_to=recipient,
                        status=BillReminder.ReminderStatus.PENDING,
                        is_sent=False,
                    )
                    updated_reminders.append(new_reminder)

        return updated_reminders

    @classmethod
    def cancel_pending_reminders(cls, bill):
        """
        Transitions all unsent/pending reminders to CANCELLED status when a bill is paid.
        Preserves delivery history and database records for complete auditing.
        """
        updated_count = bill.reminders.filter(
            status__in=[BillReminder.ReminderStatus.PENDING, BillReminder.ReminderStatus.PROCESSING],
            is_sent=False,
        ).update(status=BillReminder.ReminderStatus.CANCELLED)
        return updated_count
