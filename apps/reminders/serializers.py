from rest_framework import serializers
from .models import (
    BillReminder,
    NotificationLog,
    NotificationPreference,
    InAppNotification,
    NotificationDelivery,
)


class BillReminderSerializer(serializers.ModelSerializer):
    """Serializer for scheduled bill reminders."""
    bill_provider = serializers.CharField(source="bill.provider_name", read_only=True)
    bill_amount = serializers.DecimalField(source="bill.amount", max_digits=10, decimal_places=2, read_only=True)
    bill_due_date = serializers.DateField(source="bill.due_date", read_only=True)
    sent_to_name = serializers.CharField(source="sent_to.display_name", read_only=True)

    class Meta:
        model = BillReminder
        fields = (
            "id",
            "bill",
            "bill_provider",
            "bill_amount",
            "bill_due_date",
            "remind_at",
            "reminder_type",
            "status",
            "is_sent",
            "sent_at",
            "sent_to",
            "sent_to_name",
            "retry_count",
            "error_message",
            "created_at",
        )
        read_only_fields = ("id", "status", "is_sent", "sent_at", "retry_count", "error_message", "created_at")


class NotificationLogSerializer(serializers.ModelSerializer):
    """Serializer for notification audit logs."""
    class Meta:
        model = NotificationLog
        fields = (
            "id",
            "bill",
            "reminder",
            "recipient_email",
            "subject",
            "channel",
            "status",
            "error_message",
            "created_at",
        )
        read_only_fields = fields


class NotificationPreferenceSerializer(serializers.ModelSerializer):
    """Serializer for managing user notification channel and milestone preferences."""
    class Meta:
        model = NotificationPreference
        fields = (
            "id",
            "user",
            "email_enabled",
            "in_app_enabled",
            "whatsapp_enabled",
            "whatsapp_phone_number",
            "remind_7_days_before",
            "remind_3_days_before",
            "remind_1_day_before",
            "remind_on_due_date",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "user", "created_at", "updated_at")

    def validate_whatsapp_phone_number(self, value):
        from apps.reminders.services.channels.whatsapp import WhatsAppChannel
        if value and not WhatsAppChannel.validate_phone_number(value):
            raise serializers.ValidationError(
                "Phone number must be a valid international format (e.g. +14155552671 or +919876543210)."
            )
        return value


class InAppNotificationSerializer(serializers.ModelSerializer):
    """Serializer for in-app alert inbox items."""
    bill_provider = serializers.CharField(source="bill.provider_name", read_only=True)
    bill_amount = serializers.DecimalField(source="bill.amount", max_digits=10, decimal_places=2, read_only=True)
    bill_due_date = serializers.DateField(source="bill.due_date", read_only=True)

    class Meta:
        model = InAppNotification
        fields = (
            "id",
            "bill",
            "bill_provider",
            "bill_amount",
            "bill_due_date",
            "reminder",
            "title",
            "message",
            "is_read",
            "created_at",
        )
        read_only_fields = ("id", "bill", "bill_provider", "bill_amount", "bill_due_date", "reminder", "title", "message", "created_at")


class NotificationDeliverySerializer(serializers.ModelSerializer):
    """Serializer for auditing multi-channel delivery attempts."""
    recipient_username = serializers.CharField(source="recipient.username", read_only=True)

    class Meta:
        model = NotificationDelivery
        fields = (
            "id",
            "reminder",
            "bill",
            "recipient",
            "recipient_username",
            "channel",
            "status",
            "recipient_address",
            "subject",
            "provider_response_id",
            "error_message",
            "attempt_number",
            "created_at",
        )
        read_only_fields = fields

