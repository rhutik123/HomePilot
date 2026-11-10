from django.db import models
from django.conf import settings


class ReminderType(models.TextChoices):
    THREE_DAYS_BEFORE = "THREE_DAYS_BEFORE", "3 Days Before"
    ONE_DAY_BEFORE = "ONE_DAY_BEFORE", "1 Day Before"
    DUE_DATE = "DUE_DATE", "On Due Date"
    OVERDUE = "OVERDUE", "Overdue Alert"


class BillReminder(models.Model):
    """
    Scheduled reminder for an individual bill payment milestone.
    """
    bill = models.ForeignKey(
        "bills.Bill",
        on_delete=models.CASCADE,
        related_name="reminders",
        help_text="The bill to be reminded about.",
    )
    remind_at = models.DateField(
        db_index=True,
        help_text="Target calendar date when this reminder should fire.",
    )
    reminder_type = models.CharField(
        max_length=30,
        choices=ReminderType.choices,
    )
    is_sent = models.BooleanField(
        default=False,
        db_index=True,
        help_text="True if reminder was successfully dispatched.",
    )
    sent_at = models.DateTimeField(null=True, blank=True)
    sent_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="received_reminders",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "homepilot_bill_reminders"
        verbose_name = "Bill Reminder"
        verbose_name_plural = "Bill Reminders"
        unique_together = ("bill", "reminder_type")  # Strictly prevents duplicate milestone reminders
        ordering = ["remind_at", "id"]

    def __str__(self):
        return f"Reminder ({self.reminder_type}) for {self.bill.provider_name} on {self.remind_at}"


class NotificationLog(models.Model):
    """
    Persistent audit trail of all email and notification dispatch attempts.
    """
    class Status(models.TextChoices):
        SUCCESS = "SUCCESS", "Success"
        FAILED = "FAILED", "Failed"

    reminder = models.ForeignKey(
        BillReminder,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="logs",
    )
    bill = models.ForeignKey(
        "bills.Bill",
        on_delete=models.CASCADE,
        related_name="notification_logs",
    )
    recipient_email = models.EmailField()
    subject = models.CharField(max_length=255)
    channel = models.CharField(max_length=20, default="EMAIL")
    status = models.CharField(max_length=20, choices=Status.choices)
    error_message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "homepilot_notification_logs"
        verbose_name = "Notification Log"
        verbose_name_plural = "Notification Logs"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.channel} to {self.recipient_email} - {self.status}"
