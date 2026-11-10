from rest_framework import serializers
from apps.audit.models import AuditEvent


class AuditEventSerializer(serializers.ModelSerializer):
    actor_username = serializers.CharField(source="actor.username", read_only=True)
    actor_name = serializers.CharField(source="actor.display_name", read_only=True)
    action_display = serializers.CharField(source="get_action_display", read_only=True)

    class Meta:
        model = AuditEvent
        fields = [
            "id",
            "action",
            "action_display",
            "actor",
            "actor_username",
            "actor_name",
            "household",
            "entity_type",
            "entity_id",
            "description",
            "metadata",
            "ip_address",
            "created_at",
        ]
        read_only_fields = fields
