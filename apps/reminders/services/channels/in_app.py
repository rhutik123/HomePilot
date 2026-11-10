import logging
import uuid
from typing import Tuple
from apps.reminders.models import InAppNotification

logger = logging.getLogger(__name__)


class InAppChannel:
    """
    In-app dashboard notification channel.
    Creates an InAppNotification entity for immediate display in the user's inbox/dashboard.
    """
    @classmethod
    def send(cls, reminder, recipient) -> Tuple[bool, str, str]:
        if not recipient:
            return False, "", "Recipient user is required for in-app notifications."

        bill = reminder.bill
        type_display = reminder.get_reminder_type_display() if hasattr(reminder, "get_reminder_type_display") else reminder.reminder_type
        title = f"Bill Due: {bill.provider_name} ({type_display})"
        message = (
            f"A payment of {bill.amount} {bill.currency} is due on {bill.due_date} "
            f"for household '{bill.household.name}'."
        )

        try:
            notification = InAppNotification.objects.create(
                user=recipient,
                bill=bill,
                reminder=reminder,
                title=title,
                message=message,
                is_read=False,
            )
            delivery_id = f"inapp_{notification.id}_{uuid.uuid4().hex[:6]}"
            return True, delivery_id, ""
        except Exception as exc:
            error_msg = f"Failed to create in-app notification: {str(exc)}"
            logger.exception(error_msg)
            return False, "", error_msg
