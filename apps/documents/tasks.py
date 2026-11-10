import logging
from decimal import Decimal
from datetime import datetime
from celery import shared_task
from django.utils import timezone
from django.db import transaction

from .models import DocumentProcessingJob, ExtractionResult
from apps.bills.models import Bill
from apps.bills.services.document_parser import DocumentParserService
from apps.bills.services.extraction import get_extraction_provider

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=2, default_retry_delay=30)
def process_document_job_task(self, job_id: int):
    """
    Celery background worker task to process an uploaded bill document:
    1. Extracts text via OCR / digital PDF parser.
    2. Runs AI extraction (Gemini or Mock provider).
    3. Detects potential duplicate bills in the household.
    4. Stores structured ExtractionResult and updates job status.
    """
    try:
        job = DocumentProcessingJob.objects.select_related("document", "document__household").get(id=job_id)
    except DocumentProcessingJob.DoesNotExist:
        logger.error(f"DocumentProcessingJob {job_id} not found.")
        return False

    job.status = DocumentProcessingJob.Status.PROCESSING
    job.attempts += 1
    job.save(update_fields=["status", "attempts"])

    document = job.document

    try:
        # 1. Extract raw text via OCR & PDF Parser
        raw_text = DocumentParserService.extract_text(document.file, mime_type=document.mime_type)

        # 2. Run configured AI provider
        provider = get_extraction_provider()
        provider_name = getattr(provider, "MODEL_NAME", "mock")
        extracted_result = provider.extract(raw_text, mime_type=document.mime_type)
        extracted_dict = extracted_result.model_dump(mode="json")

        # 3. Duplicate Bill Detection
        possible_duplicate = False
        duplicate_reason = ""

        amount_val = extracted_result.amount
        due_val = extracted_result.due_date
        provider_val = extracted_result.provider_name

        if amount_val and due_val and provider_val:
            existing_match = Bill.objects.filter(
                household=document.household,
                provider_name__iexact=provider_val.strip(),
                amount=amount_val,
                due_date=due_val,
            ).first()

            if existing_match:
                possible_duplicate = True
                duplicate_reason = (
                    f"Possible duplicate of existing Bill #{existing_match.id} "
                    f"('{existing_match.provider_name}' for {existing_match.amount} {existing_match.currency} "
                    f"due on {existing_match.due_date})."
                )

        # 4. Save ExtractionResult and complete job atomically
        with transaction.atomic():
            confidence_score = Decimal(str(extracted_result.overall_confidence or "0.00"))
            ExtractionResult.objects.create(
                document=document,
                job=job,
                raw_ocr_text=raw_text,
                extracted_data=extracted_dict,
                confidence_score=confidence_score,
                provider_name=provider.__class__.__name__,
                model_version=provider_name,
                possible_duplicate=possible_duplicate,
                duplicate_reason=duplicate_reason,
            )

            job.status = DocumentProcessingJob.Status.SUCCESS
            job.completed_at = timezone.now()
            job.save(update_fields=["status", "completed_at"])

        logger.info(f"Successfully processed DocumentProcessingJob #{job.id} for {document.file_name}.")
        return True

    except Exception as exc:
        logger.error(f"Error processing DocumentProcessingJob #{job.id}: {exc}")
        job.status = DocumentProcessingJob.Status.FAILED
        job.error_message = str(exc)
        job.save(update_fields=["status", "error_message"])

        # Retry transient failures
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc)
        return False
