from django.db import models
from django.conf import settings
from apps.bills.validators import validate_bill_document


class Document(models.Model):
    """
    Core document model for physical/scanned bills, PDFs, and receipts.
    Independent entity representing an uploaded file within a household.
    """
    household = models.ForeignKey(
        "households.Household",
        on_delete=models.CASCADE,
        related_name="household_documents",
        help_text="Tenant boundary for this document.",
    )
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="uploaded_documents",
    )
    file = models.FileField(
        upload_to="documents/%Y/%m/",
        validators=[validate_bill_document],
    )
    file_name = models.CharField(max_length=255)
    file_size = models.PositiveIntegerField(help_text="Size in bytes")
    file_hash = models.CharField(
        max_length=64,
        db_index=True,
        help_text="SHA-256 hash of file content to detect duplicates within household.",
    )
    mime_type = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "homepilot_documents"
        verbose_name = "Document"
        verbose_name_plural = "Documents"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["household", "file_hash"]),
        ]

    def __str__(self):
        return f"{self.file_name} ({self.household.name})"


class DocumentProcessingJob(models.Model):
    """
    Tracks asynchronous Celery processing jobs for document OCR and AI extraction.
    """
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        PROCESSING = "PROCESSING", "Processing"
        SUCCESS = "SUCCESS", "Success"
        FAILED = "FAILED", "Failed"

    document = models.ForeignKey(
        Document,
        on_delete=models.CASCADE,
        related_name="processing_jobs",
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    attempts = models.PositiveIntegerField(default=0)
    error_message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "homepilot_document_processing_jobs"
        verbose_name = "Document Processing Job"
        verbose_name_plural = "Document Processing Jobs"
        ordering = ["-created_at"]

    def __str__(self):
        return f"Job #{self.id} for {self.document.file_name} [{self.status}]"


class ExtractionResult(models.Model):
    """
    Preserves structured data extracted from a document by OCR and AI providers.
    """
    document = models.ForeignKey(
        Document,
        on_delete=models.CASCADE,
        related_name="extraction_results",
    )
    job = models.ForeignKey(
        DocumentProcessingJob,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="extraction_results",
    )
    raw_ocr_text = models.TextField(blank=True, help_text="Text extracted by OCR/PDF parser.")
    extracted_data = models.JSONField(default=dict, blank=True, help_text="Structured JSON parsed by AI.")
    confidence_score = models.DecimalField(
        max_digits=4,
        decimal_places=2,
        default=0.00,
        help_text="Overall confidence score (0.00 to 1.00).",
    )
    provider_name = models.CharField(max_length=50, default="mock")
    model_version = models.CharField(max_length=50, default="mock-v1")
    possible_duplicate = models.BooleanField(default=False)
    duplicate_reason = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "homepilot_extraction_results"
        verbose_name = "Extraction Result"
        verbose_name_plural = "Extraction Results"
        ordering = ["-created_at"]

    def __str__(self):
        return f"Extraction for {self.document.file_name} ({self.provider_name}: {self.confidence_score})"


class BillDocumentLink(models.Model):
    """
    Through-model linking confirmed Bills to one or more supporting Documents.
    """
    bill = models.ForeignKey(
        "bills.Bill",
        on_delete=models.CASCADE,
        related_name="document_links",
    )
    document = models.ForeignKey(
        Document,
        on_delete=models.CASCADE,
        related_name="bill_links",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "homepilot_bill_document_links"
        verbose_name = "Bill Document Link"
        verbose_name_plural = "Bill Document Links"
        unique_together = ("bill", "document")

    def __str__(self):
        return f"Link: Bill #{self.bill_id} <-> Doc #{self.document_id}"
