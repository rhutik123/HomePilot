from decimal import Decimal
from django.db import transaction
from django.http import FileResponse, Http404
from django.utils import timezone
from rest_framework import viewsets, status
from rest_framework.views import APIView
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser

from .models import Bill, BillDocument, BillStatus, BillCategory
from .serializers import (
    BillSerializer,
    BillDocumentSerializer,
    BillDocumentUploadSerializer,
)
from .services.document_parser import DocumentParserService
from .services.extraction import get_extraction_provider
from .services.bill_service import BillService
from apps.reminders.services.reminder_service import ReminderService
from apps.payments.services.payment_service import PaymentService
from apps.payments.models import PaymentMethod
from apps.audit.services import AuditService
from apps.audit.models import AuditEvent


class BillViewSet(viewsets.ModelViewSet):
    """
    CRUD API for household bills with strict multi-tenant isolation.
    """
    permission_classes = [IsAuthenticated]
    parser_classes = [JSONParser, MultiPartParser, FormParser]

    def get_queryset(self):
        # Strict isolation: Only bills belonging to households where user is an active member
        queryset = (
            Bill.objects.filter(household__memberships__user=self.request.user)
            .select_related("household", "uploaded_by", "assigned_to")
            .prefetch_related("documents")
            .distinct()
        )

        # Query parameter filters
        status_param = self.request.query_params.get("status")
        if status_param:
            queryset = queryset.filter(status=status_param.upper())

        category_param = self.request.query_params.get("category")
        if category_param:
            queryset = queryset.filter(category=category_param.upper())

        household_id = self.request.query_params.get("household")
        if household_id:
            queryset = queryset.filter(household_id=household_id)

        assigned_to_param = self.request.query_params.get("assigned_to")
        if assigned_to_param:
            if assigned_to_param == "me":
                queryset = queryset.filter(assigned_to=self.request.user)
            else:
                queryset = queryset.filter(assigned_to_id=assigned_to_param)

        return queryset

    def get_serializer_class(self):
        return BillSerializer

    def perform_create(self, serializer):
        household = serializer.validated_data["household"]
        if not household.is_member(self.request.user):
            raise Http404("Household not found or access denied.")

        document_id = self.request.data.get("document_id")
        doc = None
        if document_id:
            doc = BillDocument.objects.filter(id=document_id, household=household).first()

        data = serializer.validated_data
        bill = BillService.create_bill(
            household=household,
            uploaded_by=self.request.user,
            provider_name=data["provider_name"],
            amount=data["amount"],
            due_date=data["due_date"],
            category=data.get("category", BillCategory.OTHER),
            currency=data.get("currency", "INR"),
            assigned_to=data.get("assigned_to"),
            issue_date=data.get("issue_date"),
            billing_period=data.get("billing_period", ""),
            reference_number=data.get("reference_number", ""),
            notes=data.get("notes", ""),
            document=doc,
            source="API_UPLOAD" if doc else "API_MANUAL",
        )
        serializer.instance = bill

    def perform_update(self, serializer):
        bill = self.get_object()
        data = serializer.validated_data
        updated_bill = BillService.update_bill(
            bill=bill,
            user=self.request.user,
            **data,
        )
        serializer.instance = updated_bill

    @action(detail=True, methods=["post"])
    def mark_paid(self, request, pk=None):
        """Mark a bill as paid, synchronize payments ledger, and cancel future reminders."""
        bill = self.get_object()
        if bill.outstanding_balance > Decimal("0.00"):
            PaymentService.record_payment(
                bill=bill,
                user=request.user,
                amount=bill.outstanding_balance,
                payment_date=timezone.localdate(),
                payment_method=PaymentMethod.OTHER,
                notes="Marked paid via API mark_paid action.",
            )
        else:
            bill.mark_as_paid()

        AuditService.log(
            action=AuditEvent.Action.BILL_PAID,
            actor=request.user,
            household=bill.household,
            entity=bill,
            description=f"Bill '{bill.provider_name}' marked as paid by {request.user.display_name}.",
        )
        bill.refresh_from_db()
        return Response(
            {
                "message": f"Bill '{bill.provider_name}' marked as paid.",
                "bill": BillSerializer(bill).data,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=["get"])
    def upcoming(self, request):
        """Returns all pending bills due on or after today, ordered by due date."""
        today = timezone.localdate()
        bills = self.get_queryset().filter(
            status=BillStatus.PENDING,
            due_date__gte=today,
        ).order_by("due_date")
        serializer = self.get_serializer(bills, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @action(detail=False, methods=["get"])
    def overdue(self, request):
        """Returns all bills that are overdue (due_date < today and not paid)."""
        today = timezone.localdate()
        bills = self.get_queryset().filter(
            due_date__lt=today,
        ).exclude(status=BillStatus.PAID).order_by("due_date")
        serializer = self.get_serializer(bills, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @action(detail=False, methods=["get"])
    def paid(self, request):
        """Returns paid bills ordered by most recently paid."""
        bills = self.get_queryset().filter(
            status=BillStatus.PAID,
        ).order_by("-paid_at", "-updated_at")
        serializer = self.get_serializer(bills, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)


class BillDocumentUploadAPIView(APIView):
    """
    Endpoint for uploading a bill document file (PDF / image).
    Validates file format, size, content header, and household membership.
    """
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        serializer = BillDocumentUploadSerializer(
            data=request.data,
            context={"request": request},
        )
        if serializer.is_valid():
            document = serializer.save()
            AuditService.log(
                action=AuditEvent.Action.DOCUMENT_UPLOADED,
                actor=request.user,
                household=document.household,
                entity=document,
                description=f"Document '{document.file_name}' uploaded.",
                metadata={"file_name": document.file_name, "file_size": document.file_size, "mime_type": document.mime_type},
            )
            return Response(
                {
                    "message": "Document uploaded successfully.",
                    "document": BillDocumentSerializer(document).data,
                },
                status=status.HTTP_201_CREATED,
            )
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class BillDocumentExtractAPIView(APIView):
    """
    Extracts text from document and triggers AI/mock structured extraction.
    Step 1 of the review flow: extracts data without saving to final Bill table.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        try:
            document = BillDocument.objects.select_related("household").get(
                id=pk,
                household__memberships__user=request.user,
            )
        except BillDocument.DoesNotExist:
            return Response(
                {"error": "Document not found or access denied."},
                status=status.HTTP_404_NOT_FOUND,
            )

        try:
            # 1. Extract raw text from file
            raw_text = DocumentParserService.extract_text(
                document.file,
                mime_type=document.mime_type,
            )
            document.raw_text = raw_text

            # 2. Extract structured data via provider
            provider = get_extraction_provider()
            extracted_result = provider.extract(raw_text, mime_type=document.mime_type)

            # 3. Save extracted state on document
            extracted_dict = extracted_result.model_dump(mode="json")
            document.extracted_data = extracted_dict
            document.extraction_status = BillDocument.ExtractionStatus.SUCCESS
            document.save(update_fields=["raw_text", "extracted_data", "extraction_status"])

            AuditService.log(
                action=AuditEvent.Action.EXTRACTION_COMPLETED,
                actor=request.user,
                household=document.household,
                entity=document,
                description=f"AI extraction completed for '{document.file_name}'.",
                metadata={"missing_fields": extracted_result.missing_fields, "provider": provider.__class__.__name__},
            )

            return Response(
                {
                    "message": "Information extracted successfully. Please review before saving.",
                    "document_id": document.id,
                    "extracted_data": extracted_dict,
                    "requires_manual_entry": len(extracted_result.missing_fields) > 0,
                    "missing_fields": extracted_result.missing_fields,
                },
                status=status.HTTP_200_OK,
            )
        except Exception as e:
            document.extraction_status = BillDocument.ExtractionStatus.FAILED
            document.save(update_fields=["extraction_status"])
            AuditService.log(
                action=AuditEvent.Action.EXTRACTION_FAILED,
                actor=request.user,
                household=document.household,
                entity=document,
                description=f"AI extraction failed for '{document.file_name}': {str(e)}",
                metadata={"error": str(e)},
            )
            return Response(
                {
                    "error": f"Extraction failed: {str(e)}",
                    "requires_manual_entry": True,
                },
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )


class BillDocumentConfirmAPIView(APIView):
    """
    Step 2 of the review flow: User submits reviewed and corrected information.
    Creates the permanent Bill record and attaches the document.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        try:
            document = BillDocument.objects.select_related("household").get(
                id=pk,
                household__memberships__user=request.user,
            )
        except BillDocument.DoesNotExist:
            return Response(
                {"error": "Document not found or access denied."},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Merge household ID from document
        data = request.data.copy()
        data["household"] = document.household.id

        serializer = BillSerializer(data=data, context={"request": request})
        if serializer.is_valid():
            vdata = serializer.validated_data
            bill = BillService.create_bill(
                household=document.household,
                uploaded_by=request.user,
                provider_name=vdata["provider_name"],
                amount=vdata["amount"],
                due_date=vdata["due_date"],
                category=vdata.get("category", BillCategory.OTHER),
                currency=vdata.get("currency", "INR"),
                assigned_to=vdata.get("assigned_to"),
                issue_date=vdata.get("issue_date"),
                billing_period=vdata.get("billing_period", ""),
                reference_number=vdata.get("reference_number", ""),
                notes=vdata.get("notes", ""),
                document=document,
                source="API_DOCUMENT_CONFIRM",
            )

            return Response(
                {
                    "message": "Bill confirmed and saved successfully.",
                    "bill": BillSerializer(bill).data,
                },
                status=status.HTTP_201_CREATED,
            )
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class BillDocumentDetailAPIView(APIView):
    """
    Endpoint for inspecting uploaded document status and AI extracted data.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        try:
            document = BillDocument.objects.select_related("household").get(
                id=pk,
                household__memberships__user=request.user,
            )
        except BillDocument.DoesNotExist:
            return Response(
                {"error": "Document not found or access denied."},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(BillDocumentSerializer(document).data, status=status.HTTP_200_OK)


class BillDocumentDownloadAPIView(APIView):
    """
    Secure file streaming endpoint.
    Strictly verifies household membership before streaming the file.
    Prevents unauthorized direct access to sensitive bills.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        try:
            document = BillDocument.objects.select_related("household").get(
                id=pk,
                household__memberships__user=request.user,
            )
        except BillDocument.DoesNotExist:
            raise Http404("Document not found.")

        if not document.file:
            raise Http404("Document file missing.")

        return FileResponse(
            document.file.open("rb"),
            content_type=document.mime_type or "application/octet-stream",
            as_attachment=False,
            filename=document.file_name,
        )
