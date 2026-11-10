from django.contrib import admin
from .models import RecurringSchedule, RecurringOccurrence


@admin.register(RecurringSchedule)
class RecurringScheduleAdmin(admin.ModelAdmin):
    list_display = ("id", "provider_name", "household", "frequency", "expected_amount", "next_due_date", "is_active", "created_at")
    list_filter = ("frequency", "is_active", "household")
    search_fields = ("provider_name", "notes")


@admin.register(RecurringOccurrence)
class RecurringOccurrenceAdmin(admin.ModelAdmin):
    list_display = ("id", "schedule", "bill", "period_identifier", "generated_at")
    list_filter = ("period_identifier", "generated_at")
