from django.db import transaction
from django.http import FileResponse, Http404
from django.utils import timezone
from rest_framework import viewsets, status
from rest_framework.views import APIView
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser

from .models import Bill, BillDocument, BillStatus
from .serializers import (
    BillSerializer,
    BillDocumentSerializer,
    BillDocumentUploadSerializer,
)
from .services.document_parser import DocumentParserService
from .services.extraction import get_extraction_provider


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
        # Verify user is a member of the target household
        household = serializer.validated_data["household"]
        if not household.is_member(self.request.user):
            raise Http404("Household not found or access denied.")
        bill = serializer.save(uploaded_by=self.request.user)

        # If a document_id was passed, link that document to this bill
        document_id = self.request.data.get("document_id")
        if document_id:
            try:
                document = BillDocument.objects.get(
                    id=document_id,
                    household=household,
                )
                document.bill = bill
                document.save(update_fields=["bill"])
            except BillDocument.DoesNotExist:
                pass

    @action(detail=True, methods=["post"])
    def mark_paid(self, request, pk=None):
        """Mark a bill as paid and cancel future reminders."""
        bill = self.get_object()
        bill.mark_as_paid()
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
            with transaction.atomic():
                bill = serializer.save(uploaded_by=request.user)
                document.bill = bill
                document.save(update_fields=["bill"])

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
