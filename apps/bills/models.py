from django.db import models
from django.conf import settings
from django.utils import timezone
from apps.bills.validators import validate_bill_document


class BillCategory(models.TextChoices):
    ELECTRICITY = "ELECTRICITY", "Electricity"
    WATER = "WATER", "Water"
    GAS = "GAS", "Gas"
    INTERNET = "INTERNET", "Internet"
    MOBILE = "MOBILE", "Mobile"
    OTHER = "OTHER", "Other Utility"


class BillStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    PAID = "PAID", "Paid"
    OVERDUE = "OVERDUE", "Overdue"


class Bill(models.Model):
    """
    Core Bill model representing an expense/utility payment for a household.
    """
    household = models.ForeignKey(
        "households.Household",
        on_delete=models.CASCADE,
        related_name="bills",
        help_text="The household that owns this bill.",
    )
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="uploaded_bills",
        help_text="User who created or uploaded the bill.",
    )
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_bills",
        help_text="Household member responsible for paying this bill.",
    )

    # Core Bill Information
    provider_name = models.CharField(
        max_length=150,
        help_text="Name of the service provider (e.g. City Power, Comcast)."
    )
    category = models.CharField(
        max_length=50,
        choices=BillCategory.choices,
        default=BillCategory.OTHER,
    )
    amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        help_text="Exact bill amount in local currency.",
    )
    currency = models.CharField(max_length=10, default="INR")
    billing_period = models.CharField(
        max_length=100,
        blank=True,
        help_text="e.g. October 2026, Q3 2026",
    )
    issue_date = models.DateField(null=True, blank=True)
    due_date = models.DateField(
        db_index=True,
        help_text="Payment deadline. Used for scheduling reminders.",
    )
    reference_number = models.CharField(
        max_length=100,
        blank=True,
        help_text="Account, consumer, or invoice reference number.",
    )
    notes = models.TextField(blank=True)

    # Lifecycle & Payment Tracking
    status = models.CharField(
        max_length=20,
        choices=BillStatus.choices,
        default=BillStatus.PENDING,
        db_index=True,
    )
    paid_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "homepilot_bills"
        verbose_name = "Bill"
        verbose_name_plural = "Bills"
        ordering = ["due_date", "-created_at"]
        indexes = [
            models.Index(fields=["household", "status"]),
            models.Index(fields=["due_date", "status"]),
        ]

    def __str__(self):
        return f"{self.provider_name} - {self.amount} {self.currency} (Due: {self.due_date})"

    def mark_as_paid(self):
        """Marks the bill as paid, preserving timestamp."""
        self.status = BillStatus.PAID
        self.paid_at = timezone.now()
        self.save(update_fields=["status", "paid_at", "updated_at"])

        # Stop future pending reminders (Phase 5 integration)
        if hasattr(self, "reminders"):
            self.reminders.filter(is_sent=False).delete()

    @property
    def is_overdue(self):
        """Checks whether the bill has passed its due date without being paid."""
        if self.status == BillStatus.PAID:
            return False
        return self.due_date < timezone.localdate()


class BillDocument(models.Model):
    """
    Stores original uploaded bill documents (PDFs and images).
    Maintains document metadata and raw extracted text from OCR.
    """
    class ExtractionStatus(models.TextChoices):
        PENDING = "PENDING", "Pending"
        SUCCESS = "SUCCESS", "Success"
        FAILED = "FAILED", "Failed"

    bill = models.ForeignKey(
        Bill,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="documents",
        help_text="Associated confirmed bill. Null while draft/reviewing.",
    )
    household = models.ForeignKey(
        "households.Household",
        on_delete=models.CASCADE,
        related_name="documents",
    )
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
    )
    file = models.FileField(
        upload_to="bills/%Y/%m/",
        validators=[validate_bill_document],
    )
    file_name = models.CharField(max_length=255)
    file_size = models.PositiveIntegerField(help_text="Size in bytes")
    mime_type = models.CharField(max_length=100)
    raw_text = models.TextField(blank=True, help_text="Text extracted from file.")
    extraction_status = models.CharField(
        max_length=20,
        choices=ExtractionStatus.choices,
        default=ExtractionStatus.PENDING,
    )
    extracted_data = models.JSONField(
        default=dict,
        blank=True,
        help_text="Structured JSON parsed by the AI extraction service.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "homepilot_bill_documents"
        verbose_name = "Bill Document"
        verbose_name_plural = "Bill Documents"
        ordering = ["-created_at"]

    def __str__(self):
        return f"Document: {self.file_name} ({self.extraction_status})"
