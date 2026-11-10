from datetime import timedelta
from django.utils import timezone
from apps.reminders.models import BillReminder, ReminderType


class ReminderService:
    """
    Manages the creation, scheduling, and lifecycle of bill reminders.
    """
    @classmethod
    def create_reminders_for_bill(cls, bill):
        """
        Generates payment reminders for a confirmed bill according to milestone rules:
        - 3 days prior to due date
        - 1 day prior to due date
        - On the payment due date itself
        Only creates reminders for dates that have not already elapsed.
        """
        today = timezone.localdate()
        due_date = bill.due_date

        if not due_date:
            return []

        # Target recipient: assigned family member, or household owner
        recipient = bill.assigned_to or bill.household.created_by

        milestones = [
            (due_date - timedelta(days=3), ReminderType.THREE_DAYS_BEFORE),
            (due_date - timedelta(days=1), ReminderType.ONE_DAY_BEFORE),
            (due_date, ReminderType.DUE_DATE),
        ]

        created_reminders = []
        for remind_date, reminder_type in milestones:
            # Only schedule future or current reminders
            if remind_date >= today:
                reminder, _ = BillReminder.objects.get_or_create(
                    bill=bill,
                    reminder_type=reminder_type,
                    defaults={
                        "remind_at": remind_date,
                        "sent_to": recipient,
                    },
                )
                created_reminders.append(reminder)

        return created_reminders

    @classmethod
    def reschedule_reminders_for_bill(cls, bill):
        """
        Defect #5 Fix: Recalculates milestone remind_at dates and recipient
        for all unsent reminders when a bill's due_date or assigned_to changes.
        """
        today = timezone.localdate()
        due_date = bill.due_date
        if not due_date:
            return []

        recipient = bill.assigned_to or bill.household.created_by
        milestones = [
            (due_date - timedelta(days=3), ReminderType.THREE_DAYS_BEFORE),
            (due_date - timedelta(days=1), ReminderType.ONE_DAY_BEFORE),
            (due_date, ReminderType.DUE_DATE),
        ]

        updated_reminders = []
        for remind_date, reminder_type in milestones:
            reminder = BillReminder.objects.filter(bill=bill, reminder_type=reminder_type).first()
            if reminder:
                if not reminder.is_sent:
                    reminder.remind_at = remind_date
                    reminder.sent_to = recipient
                    reminder.save(update_fields=["remind_at", "sent_to"])
                    updated_reminders.append(reminder)
            else:
                # If milestone is future/today and does not yet exist, create it
                if remind_date >= today:
                    new_reminder = BillReminder.objects.create(
                        bill=bill,
                        reminder_type=reminder_type,
                        remind_at=remind_date,
                        sent_to=recipient,
                    )
                    updated_reminders.append(new_reminder)

        return updated_reminders

    @classmethod
    def cancel_pending_reminders(cls, bill):
        """
        Removes all unsent reminders when a bill is marked as paid.
        """
        count, _ = bill.reminders.filter(is_sent=False).delete()
        return count
