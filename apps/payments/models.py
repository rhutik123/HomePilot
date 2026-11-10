from decimal import Decimal
from django.db import models
from django.conf import settings
from django.utils import timezone
from apps.bills.validators import validate_bill_document


class PaymentMethod(models.TextChoices):
    UPI = "UPI", "UPI"
    BANK_TRANSFER = "BANK_TRANSFER", "Bank Transfer / NEFT / IMPS"
    CREDIT_CARD = "CREDIT_CARD", "Credit Card"
    DEBIT_CARD = "DEBIT_CARD", "Debit Card"
    NET_BANKING = "NET_BANKING", "Net Banking"
    CASH = "CASH", "Cash"
    OTHER = "OTHER", "Other"


class Payment(models.Model):
    """
    Granular payment transaction recorded against a household bill.
    Supports full, partial, and multiple payments against a single bill.
    """
    bill = models.ForeignKey(
        "bills.Bill",
        on_delete=models.CASCADE,
        related_name="payments",
        help_text="The bill being settled by this payment.",
    )
    household = models.ForeignKey(
        "households.Household",
        on_delete=models.CASCADE,
        related_name="household_payments",
        help_text="Tenant boundary for isolation.",
    )
    paid_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="payments_made",
        help_text="User who made or recorded this payment.",
    )
    amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        help_text="Payment amount.",
    )
    payment_date = models.DateField(
        default=timezone.localdate,
        help_text="Date the payment was completed.",
    )
    payment_method = models.CharField(
        max_length=30,
        choices=PaymentMethod.choices,
        default=PaymentMethod.UPI,
    )
    transaction_reference = models.CharField(
        max_length=150,
        blank=True,
        help_text="UTR, transaction ID, or bank reference number.",
    )
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "homepilot_payments"
        verbose_name = "Payment"
        verbose_name_plural = "Payments"
        ordering = ["-payment_date", "-created_at"]
        indexes = [
            models.Index(fields=["household", "payment_date"]),
            models.Index(fields=["bill", "payment_date"]),
        ]

    def __str__(self):
        return f"Payment of {self.amount} for {self.bill.provider_name} on {self.payment_date}"


class PaymentReceipt(models.Model):
    """
    Uploaded receipt image or PDF proving payment execution.
    """
    payment = models.OneToOneField(
        Payment,
        on_delete=models.CASCADE,
        related_name="receipt",
    )
    file = models.FileField(
        upload_to="receipts/%Y/%m/",
        validators=[validate_bill_document],
    )
    file_name = models.CharField(max_length=255)
    file_size = models.PositiveIntegerField(help_text="Size in bytes")
    mime_type = models.CharField(max_length=100)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "homepilot_payment_receipts"
        verbose_name = "Payment Receipt"
        verbose_name_plural = "Payment Receipts"
        ordering = ["-uploaded_at"]

    def __str__(self):
        return f"Receipt: {self.file_name} for Payment #{self.payment_id}"


class PaymentAdjustment(models.Model):
    """
    Records corrections, reversals, or adjustments to a recorded payment.
    Preserves audit integrity without deleting original payment rows.
    """
    payment = models.ForeignKey(
        Payment,
        on_delete=models.CASCADE,
        related_name="adjustments",
    )
    adjusted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        help_text="Adjustment amount. Negative value represents reversal/refund.",
    )
    reason = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "homepilot_payment_adjustments"
        verbose_name = "Payment Adjustment"
        verbose_name_plural = "Payment Adjustments"
        ordering = ["-created_at"]

    def __str__(self):
        return f"Adjustment of {self.amount} on Payment #{self.payment_id} ({self.reason})"
