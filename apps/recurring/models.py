from decimal import Decimal
from django.db import models
from django.conf import settings
from apps.bills.models import BillCategory


class RecurrenceFrequency(models.TextChoices):
    MONTHLY = "MONTHLY", "Monthly"
    QUARTERLY = "QUARTERLY", "Quarterly"
    YEARLY = "YEARLY", "Yearly"


class RecurringSchedule(models.Model):
    """
    Template schedule for recurring utility and subscription bills.
    Idempotently generates bill occurrences according to its recurrence rule.
    """
    household = models.ForeignKey(
        "households.Household",
        on_delete=models.CASCADE,
        related_name="recurring_schedules",
        help_text="Tenant boundary for this recurring schedule.",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_recurring_schedules",
    )
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_recurring_schedules",
        help_text="Household member responsible for paying generated bills.",
    )
    provider_name = models.CharField(
        max_length=150,
        help_text="Name of the service provider (e.g. Netflix, City Water, Comcast).",
    )
    category = models.CharField(
        max_length=50,
        choices=BillCategory.choices,
        default=BillCategory.OTHER,
    )
    expected_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        help_text="Estimated or contracted bill amount.",
    )
    currency = models.CharField(max_length=10, default="INR")
    frequency = models.CharField(
        max_length=20,
        choices=RecurrenceFrequency.choices,
        default=RecurrenceFrequency.MONTHLY,
    )
    interval_count = models.PositiveIntegerField(
        default=1,
        help_text="Interval multiplier (e.g. 1 for every month, 3 for quarterly).",
    )
    day_of_month = models.PositiveIntegerField(
        default=1,
        help_text="Day of the month the bill is typically due (1 to 31).",
    )
    next_due_date = models.DateField(
        db_index=True,
        help_text="Next target due date for generating an upcoming bill occurrence.",
    )
    is_active = models.BooleanField(
        default=True,
        db_index=True,
        help_text="Pause or resume automatic bill generation.",
    )
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "homepilot_recurring_schedules"
        verbose_name = "Recurring Schedule"
        verbose_name_plural = "Recurring Schedules"
        ordering = ["next_due_date", "-created_at"]
        indexes = [
            models.Index(fields=["household", "is_active"]),
            models.Index(fields=["is_active", "next_due_date"]),
        ]

    def __str__(self):
        return f"Recurring: {self.provider_name} ({self.frequency}) - Next Due: {self.next_due_date}"


class RecurringOccurrence(models.Model):
    """
    Tracks generated bill instances for a recurring schedule.
    A composite unique constraint on (schedule, period_identifier) strictly guarantees
    that repeated task execution can never generate duplicate bills for the same period.
    """
    schedule = models.ForeignKey(
        RecurringSchedule,
        on_delete=models.CASCADE,
        related_name="occurrences",
    )
    bill = models.OneToOneField(
        "bills.Bill",
        on_delete=models.CASCADE,
        related_name="recurring_occurrence",
    )
    period_identifier = models.CharField(
        max_length=50,
        help_text="Unique period token (e.g. '2026-11' for monthly, '2026-Q4' for quarterly).",
    )
    generated_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "homepilot_recurring_occurrences"
        verbose_name = "Recurring Occurrence"
        verbose_name_plural = "Recurring Occurrences"
        unique_together = ("schedule", "period_identifier")
        ordering = ["-generated_at"]

    def __str__(self):
        return f"Occurrence for {self.schedule.provider_name} ({self.period_identifier})"
