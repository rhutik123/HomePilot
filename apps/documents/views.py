from django.db import transaction
from django.http import FileResponse, Http404
from rest_framework import viewsets, status
from rest_framework.views import APIView
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser

from .models import Document, DocumentProcessingJob, ExtractionResult, BillDocumentLink
from .serializers import (
    DocumentSerializer,
    DocumentUploadSerializer,
    DocumentProcessingJobSerializer,
    ExtractionResultSerializer,
)
from .tasks import process_document_job_task
from apps.bills.models import Bill
from apps.bills.serializers import BillSerializer
from apps.reminders.services.reminder_service import ReminderService


class DocumentViewSet(viewsets.ModelViewSet):
    """
    ViewSet for managing household documents (PDFs and images).
    Enforces strict multi-tenant scoping and provides asynchronous OCR/AI pipelines.
    """
    permission_classes = [IsAuthenticated]
    parser_classes = [JSONParser, MultiPartParser, FormParser]

    def get_queryset(self):
        return (
            Document.objects.filter(household__memberships__user=self.request.user)
            .select_related("household", "uploaded_by")
            .prefetch_related("extraction_results", "processing_jobs")
            .distinct()
        )

    def get_serializer_class(self):
        if self.action in ["create", "upload"]:
            return DocumentUploadSerializer
        return DocumentSerializer

    @action(detail=False, methods=["post"], parser_classes=[MultiPartParser, FormParser])
    def upload(self, request):
        """
        Uploads a bill document, checks for file-level duplicates via SHA-256 hash,
        creates a processing job, and queues Celery extraction.
        """
        serializer = DocumentUploadSerializer(data=request.data, context={"request": request})
        if serializer.is_valid():
            document = serializer.save()

            # Check if this exact file was previously uploaded in this household
            prior_count = Document.objects.filter(
                household=document.household,
                file_hash=document.file_hash,
            ).exclude(id=document.id).count()

            # Create and queue async processing job
            job = DocumentProcessingJob.objects.create(document=document)
            process_document_job_task.delay(job.id)

            response_data = DocumentSerializer(document).data
            return Response(
                {
                    "message": "Document uploaded successfully. Processing initiated.",
                    "document": response_data,
                    "job_id": job.id,
                    "is_duplicate_file": prior_count > 0,
                },
                status=status.HTTP_201_CREATED,
            )
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["post"])
    def process(self, request, pk=None):
        """Re-triggers asynchronous OCR and AI extraction on an existing document."""
        document = self.get_object()
        job = DocumentProcessingJob.objects.create(document=document)
        process_document_job_task.delay(job.id)

        return Response(
            {
                "message": "Processing job queued.",
                "job_id": job.id,
                "document_id": document.id,
            },
            status=status.HTTP_202_ACCEPTED,
        )

    @action(detail=True, methods=["get"])
    def download(self, request, pk=None):
        """Securely streams document bytes after verifying household tenant authorization."""
        document = self.get_object()
        if not document.file:
            raise Http404("Document file not found.")

        return FileResponse(
            document.file.open("rb"),
            content_type=document.mime_type or "application/octet-stream",
            as_attachment=False,
            filename=document.file_name,
        )

    @action(detail=True, methods=["post"])
    def confirm(self, request, pk=None):
        """
        User reviews and confirms the extracted bill data.
        Creates the confirmed Bill record, links the document, and generates reminders.
        """
        document = self.get_object()
        data = request.data.copy()
        data["household"] = document.household.id

        serializer = BillSerializer(data=data, context={"request": request})
        if serializer.is_valid():
            with transaction.atomic():
                bill = serializer.save(uploaded_by=request.user)
                BillDocumentLink.objects.get_or_create(bill=bill, document=document)
                ReminderService.create_reminders_for_bill(bill)

            return Response(
                {
                    "message": "Bill confirmed and saved successfully.",
                    "bill": BillSerializer(bill).data,
                    "document_id": document.id,
                },
                status=status.HTTP_201_CREATED,
            )
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
