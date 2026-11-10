from django.contrib import admin
from .models import Bill, BillDocument


class BillDocumentInline(admin.TabularInline):
    model = BillDocument
    extra = 0
    readonly_fields = ("file_name", "file_size", "mime_type", "extraction_status", "created_at")


@admin.register(Bill)
class BillAdmin(admin.ModelAdmin):
    list_display = (
        "provider_name",
        "household",
        "amount",
        "currency",
        "category",
        "due_date",
        "status",
        "assigned_to",
    )
    list_filter = ("status", "category", "due_date")
    search_fields = ("provider_name", "reference_number", "household__name")
    inlines = [BillDocumentInline]


@admin.register(BillDocument)
class BillDocumentAdmin(admin.ModelAdmin):
    list_display = ("file_name", "household", "extraction_status", "file_size", "created_at")
    list_filter = ("extraction_status",)
    search_fields = ("file_name", "household__name")
