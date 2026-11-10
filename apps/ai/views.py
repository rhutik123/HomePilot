from rest_framework import viewsets, status, generics
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated

from .models import (
    AIConversation,
    AIMessage,
    AIToolExecution,
    AIUsageRecord,
    SpendingInsight,
)
from .serializers import (
    AIConversationSerializer,
    AIConversationDetailSerializer,
    AIChatInputSerializer,
    AIMessageSerializer,
    SpendingInsightSerializer,
    AIUsageRecordSerializer,
)
from .services.assistant import AIAssistantService
from .services.anomaly_detector import SpendingAnomalyDetector
from .services.spending_insights_service import SpendingInsightsService
from apps.households.models import Household


class AIConversationViewSet(viewsets.ModelViewSet):
    """
    CRUD API for stateful AI conversations with the HomePilot assistant.
    Strictly isolated to households where the user is an active member.
    """
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        queryset = (
            AIConversation.objects.filter(household__memberships__user=self.request.user)
            .select_related("household", "user")
            .prefetch_related("messages__tool_executions")
            .distinct()
        )
        household_id = self.request.query_params.get("household")
        if household_id:
            queryset = queryset.filter(household_id=household_id)
        return queryset

    def get_serializer_class(self):
        if self.action == "retrieve":
            return AIConversationDetailSerializer
        return AIConversationSerializer

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)

    def perform_destroy(self, instance):
        """
        Enforce that only the conversation creator or household owner can delete the conversation.
        Preserves an audit record of the deletion.
        """
        if instance.user != self.request.user and not instance.household.is_owner(self.request.user):
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied("You do not have permission to delete this conversation.")
        from apps.audit.services import AuditService
        from apps.audit.models import AuditEvent
        AuditService.log(
            action=AuditEvent.Action.CONVERSATION_DELETED,
            actor=self.request.user,
            household=instance.household,
            entity=instance,
            description=f"AI conversation '{instance.title}' deleted by {self.request.user.display_name}.",
            metadata={"conversation_id": str(instance.id), "title": instance.title},
        )
        instance.delete()

    @action(detail=True, methods=["post"])
    def send_message(self, request, pk=None):
        """
        Sends a user prompt into the conversation, executes controlled tools
        within the household scope, and returns the assistant's verified response.
        """
        conversation = self.get_object()
        input_serializer = AIChatInputSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)

        prompt = input_serializer.validated_data["prompt"]

        assistant_msg, tool_executions = AIAssistantService.send_message(
            conversation=conversation,
            user=request.user,
            prompt=prompt,
        )

        return Response(
            {
                "message": AIMessageSerializer(assistant_msg).data,
                "tool_executions_count": len(tool_executions),
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["get"])
    def messages(self, request, pk=None):
        """Returns chronological chat history for the conversation."""
        conversation = self.get_object()
        msgs = conversation.messages.prefetch_related("tool_executions").all()
        serializer = AIMessageSerializer(msgs, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)


class SpendingInsightViewSet(viewsets.ReadOnlyModelViewSet):
    """
    ViewSet for inspecting proactive AI spending insights and anomaly alerts.
    """
    permission_classes = [IsAuthenticated]
    serializer_class = SpendingInsightSerializer

    def get_queryset(self):
        queryset = (
            SpendingInsight.objects.filter(household__memberships__user=self.request.user)
            .select_related("household")
            .distinct()
        )
        household_id = self.request.query_params.get("household")
        if household_id:
            queryset = queryset.filter(household_id=household_id)

        is_dismissed = self.request.query_params.get("is_dismissed")
        if is_dismissed is not None:
            queryset = queryset.filter(is_dismissed=is_dismissed.lower() in ("true", "1"))
        return queryset

    @action(detail=True, methods=["post"])
    def dismiss(self, request, pk=None):
        """Marks an insight as dismissed so it no longer clutters active dashboard views."""
        insight = self.get_object()
        insight.is_dismissed = True
        insight.save(update_fields=["is_dismissed"])
        return Response(
            {"message": f"Insight '{insight.title}' dismissed.", "is_dismissed": True},
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=["post"])
    def generate(self, request):
        """On-demand trigger to execute anomaly and trend detection across user households."""
        household_id = request.data.get("household")
        if household_id:
            try:
                household = Household.objects.get(id=household_id, memberships__user=request.user)
                households = [household]
            except Household.DoesNotExist:
                return Response({"error": "Household not found or access denied."}, status=status.HTTP_404_NOT_FOUND)
        else:
            households = Household.objects.filter(memberships__user=request.user).distinct()

        new_insights = []
        for hh in households:
            insights = SpendingInsightsService.detect_and_save_insights(hh)
            new_insights.extend(insights)

        return Response(
            {
                "message": f"Analysis complete. Generated {len(new_insights)} new insights.",
                "insights": SpendingInsightSerializer(new_insights, many=True).data,
            },
            status=status.HTTP_201_CREATED,
        )

    @action(detail=False, methods=["get"])
    def summary(self, request):
        """
        Returns full deterministic spending analysis and AI narrative explanation
        for the specified or primary household.
        """
        from django.shortcuts import get_object_or_404

        household_id = request.query_params.get("household")
        if household_id:
            household = get_object_or_404(
                Household, id=household_id, memberships__user=request.user
            )
        else:
            household = Household.objects.filter(memberships__user=request.user).first()
            if not household:
                return Response(
                    {"error": "No household found for current user."},
                    status=status.HTTP_404_NOT_FOUND,
                )

        summary_data = SpendingInsightsService.get_full_spending_summary(household)
        ai_narrative = SpendingInsightsService.generate_ai_explanation(
            summary_data, household, user=request.user
        )

        return Response(
            {
                "summary": summary_data,
                "ai_explanation": ai_narrative,
            },
            status=status.HTTP_200_OK,
        )


class AIUsageRecordListView(generics.ListAPIView):
    """
    Audit log endpoint for monitoring AI operations and token expenditure.
    """
    permission_classes = [IsAuthenticated]
    serializer_class = AIUsageRecordSerializer

    def get_queryset(self):
        return (
            AIUsageRecord.objects.filter(household__memberships__user=self.request.user)
            .select_related("household", "user")
            .distinct()
        )
