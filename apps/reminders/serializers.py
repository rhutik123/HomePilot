from rest_framework import serializers
from .models import BillReminder, NotificationLog


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
            "is_sent",
            "sent_at",
            "sent_to",
            "sent_to_name",
            "created_at",
        )
        read_only_fields = ("id", "is_sent", "sent_at", "created_at")


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
