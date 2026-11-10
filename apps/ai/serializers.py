from rest_framework import serializers
from .models import (
    AIConversation,
    AIMessage,
    AIToolExecution,
    AIUsageRecord,
    SpendingInsight,
)
from apps.households.models import Household


class AIToolExecutionSerializer(serializers.ModelSerializer):
    """Serializer representing controlled tool call execution details."""
    class Meta:
        model = AIToolExecution
        fields = (
            "id",
            "tool_name",
            "tool_arguments",
            "execution_result",
            "is_success",
            "error_message",
            "created_at",
        )
        read_only_fields = fields


class AIMessageSerializer(serializers.ModelSerializer):
    """Serializer for individual chat messages in a conversation."""
    tool_executions = AIToolExecutionSerializer(many=True, read_only=True)

    class Meta:
        model = AIMessage
        fields = (
            "id",
            "conversation",
            "role",
            "content",
            "tool_executions",
            "created_at",
        )
        read_only_fields = ("id", "conversation", "role", "tool_executions", "created_at")


class AIConversationSerializer(serializers.ModelSerializer):
    """Summary serializer for listing user AI conversations."""
    household_name = serializers.CharField(source="household.name", read_only=True)
    user_username = serializers.CharField(source="user.username", read_only=True)
    messages_count = serializers.SerializerMethodField()

    class Meta:
        model = AIConversation
        fields = (
            "id",
            "household",
            "household_name",
            "user",
            "user_username",
            "title",
            "messages_count",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "user", "household_name", "user_username", "messages_count", "created_at", "updated_at")

    def get_messages_count(self, obj):
        return obj.messages.count()

    def validate_household(self, household):
        request = self.context.get("request")
        if request and request.user.is_authenticated:
            if not household.is_member(request.user):
                raise serializers.ValidationError("You do not have permission to start a conversation for this household.")
        return household


class AIConversationDetailSerializer(AIConversationSerializer):
    """Detailed conversation serializer including chronological messages."""
    messages = AIMessageSerializer(many=True, read_only=True)

    class Meta(AIConversationSerializer.Meta):
        fields = AIConversationSerializer.Meta.fields + ("messages",)


class AIChatInputSerializer(serializers.Serializer):
    """Serializer for submitting a chat prompt to the AI assistant."""
    prompt = serializers.CharField(required=True, min_length=1, max_length=2000)


class SpendingInsightSerializer(serializers.ModelSerializer):
    """Serializer for proactive spending insights and anomaly alerts."""
    category_display = serializers.CharField(source="get_category_display", read_only=True)

    class Meta:
        model = SpendingInsight
        fields = (
            "id",
            "household",
            "insight_type",
            "title",
            "description",
            "severity",
            "category",
            "category_display",
            "amount_difference",
            "is_dismissed",
            "created_at",
        )
        read_only_fields = (
            "id",
            "household",
            "insight_type",
            "title",
            "description",
            "severity",
            "category",
            "category_display",
            "amount_difference",
            "created_at",
        )


class AIUsageRecordSerializer(serializers.ModelSerializer):
    """Audit serializer for AI token consumption and cost tracking."""
    class Meta:
        model = AIUsageRecord
        fields = (
            "id",
            "household",
            "provider",
            "model_name",
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "estimated_cost_usd",
            "operation_type",
            "created_at",
        )
        read_only_fields = fields
