from rest_framework import serializers
from .models import Document, DocumentProcessingJob, ExtractionResult, BillDocumentLink
from .services.storage import get_document_storage
from apps.accounts.serializers import UserSerializer
from apps.households.models import Household


class ExtractionResultSerializer(serializers.ModelSerializer):
    """Serializer for AI extraction results."""
    class Meta:
        model = ExtractionResult
        fields = (
            "id",
            "raw_ocr_text",
            "extracted_data",
            "confidence_score",
            "provider_name",
            "model_version",
            "possible_duplicate",
            "duplicate_reason",
            "created_at",
        )
        read_only_fields = fields


class DocumentProcessingJobSerializer(serializers.ModelSerializer):
    """Serializer for asynchronous processing jobs."""
    extraction_results = ExtractionResultSerializer(many=True, read_only=True)

    class Meta:
        model = DocumentProcessingJob
        fields = (
            "id",
            "status",
            "attempts",
            "error_message",
            "extraction_results",
            "created_at",
            "completed_at",
        )
        read_only_fields = fields


class DocumentSerializer(serializers.ModelSerializer):
    """Serializer for documents with processing status and extraction data."""
    uploaded_by = UserSerializer(read_only=True)
    latest_result = serializers.SerializerMethodField()
    latest_job_status = serializers.SerializerMethodField()

    class Meta:
        model = Document
        fields = (
            "id",
            "household",
            "uploaded_by",
            "file",
            "file_name",
            "file_size",
            "file_hash",
            "mime_type",
            "latest_job_status",
            "latest_result",
            "created_at",
            "updated_at",
        )
        read_only_fields = (
            "id",
            "uploaded_by",
            "file_name",
            "file_size",
            "file_hash",
            "mime_type",
            "latest_job_status",
            "latest_result",
            "created_at",
            "updated_at",
        )

    def get_latest_result(self, obj):
        latest = obj.extraction_results.order_by("-created_at").first()
        return ExtractionResultSerializer(latest).data if latest else None

    def get_latest_job_status(self, obj):
        job = obj.processing_jobs.order_by("-created_at").first()
        return job.status if job else None


class DocumentUploadSerializer(serializers.ModelSerializer):
    """
    Handles secure upload of bill documents:
    - Validates target household membership.
    - Computes SHA-256 hash to detect duplicate uploads within the household.
    """
    household_id = serializers.PrimaryKeyRelatedField(
        queryset=Household.objects.all(),
        source="household",
        write_only=True,
    )

    class Meta:
        model = Document
        fields = ("id", "household_id", "file", "created_at")
        read_only_fields = ("id", "created_at")

    def validate_household_id(self, household):
        request = self.context.get("request")
        if not household.is_member(request.user):
            raise serializers.ValidationError("You do not have access to upload documents for this household.")
        return household

    def create(self, validated_data):
        request = self.context.get("request")
        uploaded_file = validated_data["file"]

        # Compute SHA-256 hash using storage service
        storage = get_document_storage()
        file_hash = storage.compute_sha256(uploaded_file)

        document = Document.objects.create(
            household=validated_data["household"],
            uploaded_by=request.user,
            file=uploaded_file,
            file_name=uploaded_file.name,
            file_size=uploaded_file.size,
            file_hash=file_hash,
            mime_type=getattr(uploaded_file, "content_type", "application/octet-stream"),
        )
        return document
