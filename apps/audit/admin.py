from django.contrib import admin
from .models import AuditEvent


@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):
    list_display = ("id", "action", "actor", "household", "entity_type", "entity_id", "created_at")
    list_filter = ("action", "entity_type", "created_at")
    search_fields = ("description", "actor__username", "household__name")
    readonly_fields = ("actor", "household", "action", "entity_type", "entity_id", "description", "metadata", "ip_address", "created_at")

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
