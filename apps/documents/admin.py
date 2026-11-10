from django.contrib import admin
from .models import Document, DocumentProcessingJob, ExtractionResult, BillDocumentLink


@admin.register(Document)
class DocumentAdmin(admin.ModelAdmin):
    list_display = ("id", "file_name", "household", "uploaded_by", "file_size", "mime_type", "created_at")
    list_filter = ("mime_type", "created_at", "household")
    search_fields = ("file_name", "file_hash")


@admin.register(DocumentProcessingJob)
class DocumentProcessingJobAdmin(admin.ModelAdmin):
    list_display = ("id", "document", "status", "attempts", "created_at", "completed_at")
    list_filter = ("status", "created_at")


@admin.register(ExtractionResult)
class ExtractionResultAdmin(admin.ModelAdmin):
    list_display = ("id", "document", "provider_name", "confidence_score", "possible_duplicate", "created_at")
    list_filter = ("provider_name", "possible_duplicate", "created_at")


@admin.register(BillDocumentLink)
class BillDocumentLinkAdmin(admin.ModelAdmin):
    list_display = ("id", "bill", "document", "created_at")
