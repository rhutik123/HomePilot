from django.contrib import admin
from .models import (
    AIConversation,
    AIMessage,
    AIToolExecution,
    AIUsageRecord,
    SpendingInsight,
)


@admin.register(AIConversation)
class AIConversationAdmin(admin.ModelAdmin):
    list_display = ("id", "title", "household", "user", "created_at")
    list_filter = ("household", "created_at")
    search_fields = ("title", "user__username", "household__name")


@admin.register(AIMessage)
class AIMessageAdmin(admin.ModelAdmin):
    list_display = ("id", "conversation", "role", "created_at")
    list_filter = ("role", "created_at")
    search_fields = ("content",)


@admin.register(AIToolExecution)
class AIToolExecutionAdmin(admin.ModelAdmin):
    list_display = ("id", "tool_name", "conversation", "is_success", "created_at")
    list_filter = ("tool_name", "is_success", "created_at")


@admin.register(AIUsageRecord)
class AIUsageRecordAdmin(admin.ModelAdmin):
    list_display = ("id", "household", "user", "provider", "model_name", "total_tokens", "estimated_cost_usd", "created_at")
    list_filter = ("provider", "operation_type", "created_at")


@admin.register(SpendingInsight)
class SpendingInsightAdmin(admin.ModelAdmin):
    list_display = ("id", "title", "household", "insight_type", "severity", "is_dismissed", "created_at")
    list_filter = ("insight_type", "severity", "is_dismissed", "created_at")
    search_fields = ("title", "description", "household__name")
