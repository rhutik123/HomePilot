from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework import status
from apps.audit.models import AuditEvent
from apps.audit.serializers import AuditEventSerializer


class AuditEventListAPIView(APIView):
    """
    List audit trail events for households the authenticated user belongs to.
    Supports filtering by household_id, action, entity_type, and limit.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        qs = (
            AuditEvent.objects.filter(household__memberships__user=user)
            .select_related("actor", "household")
            .order_by("-created_at")
        )

        household_id = request.query_params.get("household_id")
        if household_id:
            qs = qs.filter(household_id=household_id)

        action = request.query_params.get("action")
        if action:
            qs = qs.filter(action=action)

        entity_type = request.query_params.get("entity_type")
        if entity_type:
            qs = qs.filter(entity_type__iexact=entity_type)

        limit = request.query_params.get("limit")
        if limit and limit.isdigit():
            qs = qs[:int(limit)]
        else:
            qs = qs[:100]

        serializer = AuditEventSerializer(qs, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)
