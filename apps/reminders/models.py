from django.db import models
from django.conf import settings


class ReminderType(models.TextChoices):
    SEVEN_DAYS_BEFORE = "SEVEN_DAYS_BEFORE", "7 Days Before"
    THREE_DAYS_BEFORE = "THREE_DAYS_BEFORE", "3 Days Before"
    ONE_DAY_BEFORE = "ONE_DAY_BEFORE", "1 Day Before"
    DUE_DATE = "DUE_DATE", "On Due Date"
    OVERDUE = "OVERDUE", "Overdue Alert"


class BillReminder(models.Model):
    """
    Scheduled reminder for an individual bill payment milestone.
    Preserves audit history and lifecycle transitions (PENDING, PROCESSING, SENT, FAILED, CANCELLED).
    """
    class ReminderStatus(models.TextChoices):
        PENDING = "PENDING", "Pending"
        PROCESSING = "PROCESSING", "Processing"
        SENT = "SENT", "Sent"
        FAILED = "FAILED", "Failed"
        CANCELLED = "CANCELLED", "Cancelled"

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
    status = models.CharField(
        max_length=20,
        choices=ReminderStatus.choices,
        default=ReminderStatus.PENDING,
        db_index=True,
        help_text="Current delivery state (PENDING, PROCESSING, SENT, FAILED, CANCELLED).",
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
    retry_count = models.PositiveIntegerField(
        default=0,
        help_text="Number of Celery delivery attempts executed.",
    )
    error_message = models.TextField(
        blank=True,
        help_text="Last recorded failure or exception details.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "homepilot_bill_reminders"
        verbose_name = "Bill Reminder"
        verbose_name_plural = "Bill Reminders"
        unique_together = ("bill", "reminder_type")  # Strictly prevents duplicate milestone reminders
        ordering = ["remind_at", "id"]

    def __str__(self):
        return f"Reminder ({self.reminder_type}) for {self.bill.provider_name} on {self.remind_at} [{self.status}]"

    def mark_sent(self):
        """Marks reminder as successfully dispatched."""
        from django.utils import timezone
        self.status = self.ReminderStatus.SENT
        self.is_sent = True
        self.sent_at = timezone.now()
        self.error_message = ""
        self.save(update_fields=["status", "is_sent", "sent_at", "error_message"])

    def mark_failed(self, error_message: str):
        """Marks reminder as failed after retries exhausted."""
        self.status = self.ReminderStatus.FAILED
        self.error_message = error_message
        self.save(update_fields=["status", "error_message"])

    def mark_cancelled(self):
        """Cancels reminder when bill is paid, preserving historical record."""
        self.status = self.ReminderStatus.CANCELLED
        self.save(update_fields=["status"])


class NotificationLog(models.Model):
    """
    Persistent audit trail of all email and notification dispatch attempts.
    Preserved permanently even if bills or reminders are deleted or modified.
    """
    class Status(models.TextChoices):
        SUCCESS = "SUCCESS", "Success"
        FAILED = "FAILED", "Failed"

    reminder = models.ForeignKey(
        BillReminder,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="logs",
        help_text="Associated reminder milestone. Preserved even if reminder is deleted.",
    )
    bill = models.ForeignKey(
        "bills.Bill",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="notification_logs",
        help_text="Associated bill. Preserved with null reference if bill is deleted.",
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


class NotificationChannel(models.TextChoices):
    EMAIL = "EMAIL", "Email"
    IN_APP = "IN_APP", "In-App Notification"
    WHATSAPP = "WHATSAPP", "WhatsApp"


class NotificationPreference(models.Model):
    """
    User settings governing which communication channels and milestones are active for reminders.
    """
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="notification_preferences",
    )
    email_enabled = models.BooleanField(default=True)
    in_app_enabled = models.BooleanField(default=True)
    whatsapp_enabled = models.BooleanField(default=False)
    whatsapp_phone_number = models.CharField(
        max_length=20,
        blank=True,
        help_text="E.164 formatted contact number for WhatsApp notifications.",
    )
    # Milestone scheduling preferences
    remind_7_days_before = models.BooleanField(default=True, help_text="Notify 7 days prior to payment due date.")
    remind_3_days_before = models.BooleanField(default=True, help_text="Notify 3 days prior to payment due date.")
    remind_1_day_before = models.BooleanField(default=True, help_text="Notify 1 day prior to payment due date.")
    remind_on_due_date = models.BooleanField(default=True, help_text="Notify on the payment due date itself.")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "homepilot_notification_preferences"
        verbose_name = "Notification Preference"
        verbose_name_plural = "Notification Preferences"

    def __str__(self):
        return f"Preferences for {self.user.username}"


class InAppNotification(models.Model):
    """
    In-app notification delivered directly into the web dashboard or UI inbox.
    Preserved permanently even if the underlying bill is deleted.
    """
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="in_app_notifications",
    )
    bill = models.ForeignKey(
        "bills.Bill",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="in_app_notifications",
        help_text="Associated bill. Preserved with null reference if bill is deleted.",
    )
    reminder = models.ForeignKey(
        BillReminder,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="in_app_alerts",
    )
    title = models.CharField(max_length=255)
    message = models.TextField()
    is_read = models.BooleanField(default=False, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "homepilot_in_app_notifications"
        verbose_name = "In-App Notification"
        verbose_name_plural = "In-App Notifications"
        ordering = ["-created_at"]

    def __str__(self):
        return f"In-App for {self.user.username}: {self.title} (Read: {self.is_read})"


class NotificationDelivery(models.Model):
    """
    Detailed delivery attempt record tracking multi-channel dispatch,
    provider responses, and retry history.
    Preserved permanently for auditing.
    """
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        PROCESSING = "PROCESSING", "Processing"
        SUCCESS = "SUCCESS", "Success"
        FAILED = "FAILED", "Failed"
        CANCELLED = "CANCELLED", "Cancelled"

    reminder = models.ForeignKey(
        BillReminder,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="deliveries",
    )
    bill = models.ForeignKey(
        "bills.Bill",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="deliveries",
        help_text="Associated bill. Preserved with null reference if bill is deleted.",
    )
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="notification_deliveries",
    )
    channel = models.CharField(max_length=20, choices=NotificationChannel.choices)
    status = models.CharField(max_length=20, choices=Status.choices)
    recipient_address = models.CharField(max_length=255, help_text="Email address or phone number.")
    subject = models.CharField(max_length=255)
    provider_response_id = models.CharField(max_length=255, blank=True)
    error_message = models.TextField(blank=True)
    attempt_number = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "homepilot_notification_deliveries"
        verbose_name = "Notification Delivery"
        verbose_name_plural = "Notification Deliveries"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.channel} to {self.recipient_address} [{self.status}]"
