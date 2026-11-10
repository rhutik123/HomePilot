from django.contrib import admin
from .models import BillReminder, NotificationLog


@admin.register(BillReminder)
class BillReminderAdmin(admin.ModelAdmin):
    list_display = ("bill", "reminder_type", "remind_at", "is_sent", "sent_to", "sent_at")
    list_filter = ("is_sent", "reminder_type", "remind_at")
    search_fields = ("bill__provider_name", "sent_to__email", "sent_to__username")


@admin.register(NotificationLog)
class NotificationLogAdmin(admin.ModelAdmin):
    list_display = ("recipient_email", "subject", "status", "created_at")
    list_filter = ("status", "channel")
    search_fields = ("recipient_email", "subject", "error_message")
