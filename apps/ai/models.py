from decimal import Decimal
from django.db import models
from django.conf import settings
from apps.bills.models import BillCategory


class AIConversation(models.Model):
    """
    Stateful conversational thread between a household member and HomePilot AI assistant.
    Strictly isolated within the household tenant boundary.
    """
    household = models.ForeignKey(
        "households.Household",
        on_delete=models.CASCADE,
        related_name="ai_conversations",
        help_text="Tenant boundary for this conversation.",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="ai_conversations",
    )
    title = models.CharField(max_length=255, default="Household Bill Assistant")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "homepilot_ai_conversations"
        verbose_name = "AI Conversation"
        verbose_name_plural = "AI Conversations"
        ordering = ["-updated_at"]

    def __str__(self):
        return f"{self.title} ({self.household.name} - {self.user.username})"


class AIMessage(models.Model):
    """
    Individual chat turn within an AI conversation.
    """
    class Role(models.TextChoices):
        USER = "USER", "User"
        ASSISTANT = "ASSISTANT", "Assistant"
        SYSTEM = "SYSTEM", "System"
        TOOL = "TOOL", "Tool"

    conversation = models.ForeignKey(
        AIConversation,
        on_delete=models.CASCADE,
        related_name="messages",
    )
    role = models.CharField(max_length=20, choices=Role.choices)
    content = models.TextField()
    tool_calls = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "homepilot_ai_messages"
        verbose_name = "AI Message"
        verbose_name_plural = "AI Messages"
        ordering = ["created_at", "id"]

    def __str__(self):
        return f"[{self.role}] {self.content[:50]}..."


class AIToolExecution(models.Model):
    """
    Audit log of controlled tool/function calls executed on behalf of the AI assistant.
    Preserves tool arguments, output, status, and error states for strict verification.
    """
    conversation = models.ForeignKey(
        AIConversation,
        on_delete=models.CASCADE,
        related_name="tool_executions",
    )
    message = models.ForeignKey(
        AIMessage,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tool_executions",
    )
    tool_name = models.CharField(max_length=100)
    tool_arguments = models.JSONField(default=dict, blank=True)
    execution_result = models.JSONField(default=dict, blank=True)
    is_success = models.BooleanField(default=True)
    error_message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "homepilot_ai_tool_executions"
        verbose_name = "AI Tool Execution"
        verbose_name_plural = "AI Tool Executions"
        ordering = ["-created_at"]

    def __str__(self):
        status_str = "SUCCESS" if self.is_success else "FAILED"
        return f"Tool {self.tool_name} [{status_str}]"


class AIUsageRecord(models.Model):
    """
    Token usage and cost tracking record for hosted AI operations.
    """
    household = models.ForeignKey(
        "households.Household",
        on_delete=models.CASCADE,
        related_name="ai_usage_records",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ai_usage_records",
    )
    provider = models.CharField(max_length=50, default="google-gemini")
    model_name = models.CharField(max_length=100, default="gemini-2.5-flash")
    prompt_tokens = models.PositiveIntegerField(default=0)
    completion_tokens = models.PositiveIntegerField(default=0)
    total_tokens = models.PositiveIntegerField(default=0)
    estimated_cost_usd = models.DecimalField(max_digits=10, decimal_places=5, default=Decimal("0.00000"))
    operation_type = models.CharField(max_length=50, default="CHAT_ASSISTANT")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "homepilot_ai_usage_records"
        verbose_name = "AI Usage Record"
        verbose_name_plural = "AI Usage Records"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.operation_type} - {self.total_tokens} tokens (${self.estimated_cost_usd})"


class SpendingInsight(models.Model):
    """
    Proactive AI insight, expense anomaly, or spending recommendation generated for a household.
    """
    class InsightType(models.TextChoices):
        ANOMALY = "ANOMALY", "Spending Anomaly"
        SAVING_OPPORTUNITY = "SAVING_OPPORTUNITY", "Saving Opportunity"
        EXPENSE_SPIKE = "EXPENSE_SPIKE", "Expense Spike"
        UPCOMING_EXPENSE_FORECAST = "UPCOMING_EXPENSE_FORECAST", "Upcoming Expense Forecast"
        CATEGORY_TREND = "CATEGORY_TREND", "Category Trend"
        RATE_INCREASE = "RATE_INCREASE", "Provider Rate Increase"
        MONTHLY_COMPARISON = "MONTHLY_COMPARISON", "Monthly Comparison"
        RECURRING_SUMMARY = "RECURRING_SUMMARY", "Recurring Commitments"

    class Severity(models.TextChoices):
        INFO = "INFO", "Informational"
        WARNING = "WARNING", "Warning"
        CRITICAL = "CRITICAL", "Critical"

    household = models.ForeignKey(
        "households.Household",
        on_delete=models.CASCADE,
        related_name="spending_insights",
    )
    insight_type = models.CharField(
        max_length=50,
        choices=InsightType.choices,
        default=InsightType.ANOMALY,
    )
    title = models.CharField(max_length=255)
    description = models.TextField()
    severity = models.CharField(
        max_length=20,
        choices=Severity.choices,
        default=Severity.INFO,
    )
    category = models.CharField(
        max_length=50,
        choices=BillCategory.choices,
        blank=True,
    )
    amount_difference = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Deviation or anticipated difference from moving average.",
    )
    is_dismissed = models.BooleanField(default=False, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "homepilot_spending_insights"
        verbose_name = "Spending Insight"
        verbose_name_plural = "Spending Insights"
        ordering = ["-created_at"]

    def __str__(self):
        return f"[{self.severity}] {self.title} ({self.household.name})"
