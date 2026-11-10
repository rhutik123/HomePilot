import logging
from django.core.mail import send_mail
from django.conf import settings
from django.utils import timezone
from apps.reminders.models import NotificationLog

logger = logging.getLogger(__name__)


class EmailNotificationService:
    """
    Handles formatting and sending email notifications for bill reminders.
    Creates persistent NotificationLog entries for auditing and observability.
    """
    @classmethod
    def send_bill_reminder(cls, reminder) -> bool:
        bill = reminder.bill
        recipient = reminder.sent_to

        if not recipient or not recipient.email:
            error_msg = f"Cannot send reminder: No recipient email specified for reminder {reminder.id}."
            logger.warning(error_msg)
            NotificationLog.objects.create(
                reminder=reminder,
                bill=bill,
                recipient_email=getattr(recipient, "email", "unknown@local"),
                subject="Missing Email",
                channel="EMAIL",
                status=NotificationLog.Status.FAILED,
                error_message=error_msg,
            )
            return False

        # Format Subject based on milestone
        type_display = reminder.get_reminder_type_display()
        subject = f"HomePilot Reminder: {bill.provider_name} bill due on {bill.due_date} ({type_display})"

        message = (
            f"Hello {recipient.display_name},\n\n"
            f"This is a friendly reminder from HomePilot for your household '{bill.household.name}'.\n\n"
            f"Bill Details:\n"
            f"- Provider: {bill.provider_name}\n"
            f"- Category: {bill.get_category_display()}\n"
            f"- Amount Due: {bill.amount} {bill.currency}\n"
            f"- Due Date: {bill.due_date}\n"
            f"- Billing Period: {bill.billing_period or 'N/A'}\n"
            f"- Reference Number: {bill.reference_number or 'N/A'}\n\n"
            f"Please remember to mark this bill as paid in HomePilot once completed.\n\n"
            f"Best regards,\n"
            f"HomePilot Assistant"
        )

        from_email = getattr(settings, "DEFAULT_FROM_EMAIL", "HomePilot <notifications@homepilot.local>")

        try:
            send_mail(
                subject=subject,
                message=message,
                from_email=from_email,
                recipient_list=[recipient.email],
                fail_silently=False,
            )

            # Record successful notification audit
            NotificationLog.objects.create(
                reminder=reminder,
                bill=bill,
                recipient_email=recipient.email,
                subject=subject,
                channel="EMAIL",
                status=NotificationLog.Status.SUCCESS,
            )
            return True
        except Exception as e:
            error_msg = f"Email delivery failed: {str(e)}"
            logger.exception(error_msg)
            NotificationLog.objects.create(
                reminder=reminder,
                bill=bill,
                recipient_email=recipient.email,
                subject=subject,
                channel="EMAIL",
                status=NotificationLog.Status.FAILED,
                error_message=error_msg,
            )
            raise e
