import logging
import uuid
from typing import Tuple
from apps.reminders.services.email_service import EmailNotificationService

logger = logging.getLogger(__name__)


class EmailChannel:
    """
    Email notification channel for delivering bill payment milestones.
    Delegates to EmailNotificationService to format messages, perform SMTP dispatch,
    and persist audit logs in NotificationLog.
    """
    @classmethod
    def send(cls, reminder, recipient, email_address: str = None) -> Tuple[bool, str, str]:
        delivery_id = f"email_{uuid.uuid4().hex[:12]}"
        try:
            success = EmailNotificationService.send_bill_reminder(reminder)
            if success:
                return True, delivery_id, ""
            return False, delivery_id, "Email delivery returned false (e.g. missing recipient or email address)."
        except Exception as exc:
            logger.error(f"EmailChannel dispatch exception: {exc}")
            raise exc
