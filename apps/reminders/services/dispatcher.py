import logging
from typing import Dict, Any
from apps.reminders.models import (
    BillReminder,
    NotificationDelivery,
    NotificationPreference,
    NotificationChannel,
)
from .channels.email import EmailChannel
from .channels.in_app import InAppChannel
from .channels.whatsapp import WhatsAppChannel

logger = logging.getLogger(__name__)


class NotificationDispatcherService:
    """
    Multi-channel notification dispatcher for bill payment milestones.
    Inspects user preferences and orchestrates dispatch across Email,
    In-App notifications, and WhatsApp with full audit trail in NotificationDelivery.
    """
    @classmethod
    def dispatch(cls, reminder: BillReminder, attempt_number: int = 1) -> Dict[str, Any]:
        bill = reminder.bill
        recipient = reminder.sent_to or bill.assigned_to or bill.household.created_by

        if not recipient:
            logger.warning(f"Reminder {reminder.id} has no recipient. Aborting dispatch.")
            return {"success": False, "channels": {}, "error": "No recipient specified."}

        # Retrieve or initialize user notification preferences
        preferences, _ = NotificationPreference.objects.get_or_create(
            user=recipient,
            defaults={
                "email_enabled": True,
                "in_app_enabled": True,
                "whatsapp_enabled": False,
            },
        )

        # Check if at least one channel is active
        active_channels = []
        if preferences.email_enabled:
            active_channels.append("EMAIL")
        if preferences.in_app_enabled:
            active_channels.append("IN_APP")
        if preferences.whatsapp_enabled:
            active_channels.append("WHATSAPP")

        if not active_channels:
            err_msg = f"User '{recipient.username}' has disabled all notification channels."
            logger.warning(f"Reminder {reminder.id}: {err_msg}")
            return {"success": False, "channels": {}, "error": err_msg}

        results = {}
        errors = []
        email_success = False
        in_app_success = False
        whatsapp_success = False

        # 1. Email Channel
        if preferences.email_enabled:
            recipient_email = getattr(recipient, "email", "") or ""
            try:
                success, delivery_id, error = EmailChannel.send(reminder, recipient, email_address=recipient_email)
                NotificationDelivery.objects.create(
                    reminder=reminder,
                    bill=bill,
                    recipient=recipient,
                    channel=NotificationChannel.EMAIL,
                    status=NotificationDelivery.Status.SUCCESS if success else NotificationDelivery.Status.FAILED,
                    recipient_address=recipient_email or "missing@local",
                    subject=f"Milestone: {reminder.reminder_type}",
                    provider_response_id=delivery_id,
                    error_message=error,
                    attempt_number=attempt_number,
                )
                results["email"] = {"success": success, "id": delivery_id, "error": error}
                email_success = success
                if not success:
                    errors.append(f"Email failed: {error}")
            except Exception as exc:
                NotificationDelivery.objects.create(
                    reminder=reminder,
                    bill=bill,
                    recipient=recipient,
                    channel=NotificationChannel.EMAIL,
                    status=NotificationDelivery.Status.FAILED,
                    recipient_address=recipient_email or "missing@local",
                    subject=f"Milestone: {reminder.reminder_type}",
                    provider_response_id="",
                    error_message=str(exc),
                    attempt_number=attempt_number,
                )
                errors.append(f"Email exception: {str(exc)}")

        # 2. In-App Notification Channel
        if preferences.in_app_enabled:
            success, delivery_id, error = InAppChannel.send(reminder, recipient)
            NotificationDelivery.objects.create(
                reminder=reminder,
                bill=bill,
                recipient=recipient,
                channel=NotificationChannel.IN_APP,
                status=NotificationDelivery.Status.SUCCESS if success else NotificationDelivery.Status.FAILED,
                recipient_address=f"user:{recipient.id}",
                subject=f"Milestone: {reminder.reminder_type}",
                provider_response_id=delivery_id,
                error_message=error,
                attempt_number=attempt_number,
            )
            results["in_app"] = {"success": success, "id": delivery_id, "error": error}
            in_app_success = success
            if not success:
                errors.append(f"In-App failed: {error}")

        # 3. WhatsApp Channel
        if preferences.whatsapp_enabled:
            target_phone = preferences.whatsapp_phone_number
            success, delivery_id, error = WhatsAppChannel.send(reminder, recipient, phone_number=target_phone)
            NotificationDelivery.objects.create(
                reminder=reminder,
                bill=bill,
                recipient=recipient,
                channel=NotificationChannel.WHATSAPP,
                status=NotificationDelivery.Status.SUCCESS if success else NotificationDelivery.Status.FAILED,
                recipient_address=target_phone or "unknown",
                subject=f"Milestone: {reminder.reminder_type}",
                provider_response_id=delivery_id,
                error_message=error,
                attempt_number=attempt_number,
            )
            results["whatsapp"] = {"success": success, "id": delivery_id, "error": error}
            whatsapp_success = success
            if not success:
                errors.append(f"WhatsApp failed: {error}")

        # Success criteria:
        # If email is enabled, it must succeed for the dispatch to be considered successful.
        # If email is disabled, at least one other enabled channel must succeed.
        if preferences.email_enabled:
            overall_success = email_success
        else:
            overall_success = in_app_success or whatsapp_success

        return {
            "success": overall_success,
            "channels": results,
            "error": "; ".join(errors) if not overall_success else "",
        }
