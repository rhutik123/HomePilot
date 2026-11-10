import logging
import uuid
import re
import urllib.request
import json
from typing import Tuple
from django.conf import settings

logger = logging.getLogger(__name__)


class WhatsAppChannel:
    """
    WhatsApp Business notification channel.
    Integrates with official WhatsApp Cloud API (Meta Graph API) when configured,
    or runs in simulated development/test mode with structured audit logging.
    """
    @classmethod
    def validate_phone_number(cls, phone: str) -> bool:
        """Validates that the phone number conforms to international E.164 format."""
        if not phone:
            return False
        # Matches +[country code][digits], 8-15 digits
        pattern = r"^\+?[1-9]\d{7,14}$"
        return bool(re.match(pattern, phone.strip()))

    @classmethod
    def send(cls, reminder, recipient, phone_number: str = None) -> Tuple[bool, str, str]:
        target_phone = phone_number
        if not target_phone and hasattr(recipient, "notification_preferences"):
            target_phone = recipient.notification_preferences.whatsapp_phone_number

        if not target_phone or not cls.validate_phone_number(target_phone):
            error_msg = f"Invalid or missing WhatsApp phone number: '{target_phone}'."
            logger.warning(error_msg)
            return False, "", error_msg

        bill = reminder.bill
        type_display = reminder.get_reminder_type_display() if hasattr(reminder, "get_reminder_type_display") else reminder.reminder_type
        message_body = (
            f"🔔 *HomePilot Reminder* ({type_display})\n\n"
            f"Household: *{bill.household.name}*\n"
            f"Provider: *{bill.provider_name}*\n"
            f"Amount Due: *{bill.amount} {bill.currency}*\n"
            f"Due Date: *{bill.due_date}*\n\n"
            f"Please mark as paid in HomePilot once completed."
        )

        api_token = getattr(settings, "WHATSAPP_API_TOKEN", None)
        phone_number_id = getattr(settings, "WHATSAPP_PHONE_NUMBER_ID", None)
        simulation_enabled = getattr(settings, "WHATSAPP_SIMULATION_ENABLED", False)

        if api_token and phone_number_id:
            # Live dispatch via Meta Graph API v18.0+
            url = f"https://graph.facebook.com/v18.0/{phone_number_id}/messages"
            payload = {
                "messaging_product": "whatsapp",
                "to": target_phone.lstrip("+"),
                "type": "text",
                "text": {"body": message_body},
            }
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {api_token}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=10) as response:
                    res_data = json.loads(response.read().decode("utf-8"))
                    msg_id = res_data.get("messages", [{}])[0].get("id", f"wa_{uuid.uuid4().hex[:12]}")
                    logger.info(f"WhatsApp Cloud API message sent to {target_phone}: ID {msg_id}")
                    return True, msg_id, ""
            except Exception as exc:
                error_msg = f"WhatsApp Cloud API dispatch failed: {str(exc)}"
                logger.exception(error_msg)
                return False, "", error_msg
        elif simulation_enabled:
            # Explicit development / sandbox simulation mode
            delivery_id = f"wa_sim_{uuid.uuid4().hex[:12]}"
            logger.info(
                f"[WHATSAPP SIMULATION] Message to {target_phone}: {message_body} (Ref: {delivery_id})"
            )
            return True, delivery_id, ""
        else:
            error_msg = (
                "WhatsApp Business Platform is not configured. Live credentials "
                "(WHATSAPP_API_TOKEN, WHATSAPP_PHONE_NUMBER_ID) and Meta-approved templates "
                "are required. Refer to docs/WHATSAPP_BUSINESS_API_SETUP.md."
            )
            logger.warning(error_msg)
            return False, "", error_msg
