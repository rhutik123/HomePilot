from django.db import models
from django.conf import settings


class AuditEvent(models.Model):
    """
    Persistent audit trail recording critical household, bill, payment,
    and security events across HomePilot.
    Historical records must never be casually deleted or purged.
    """
    class Action(models.TextChoices):
        USER_REGISTERED = "USER_REGISTERED", "User Registered"
        HOUSEHOLD_CREATED = "HOUSEHOLD_CREATED", "Household Created"
        CONVERSATION_DELETED = "CONVERSATION_DELETED", "Conversation Deleted"
        BILL_CREATED = "BILL_CREATED", "Bill Created"
        BILL_UPDATED = "BILL_UPDATED", "Bill Updated"
        BILL_PAID = "BILL_PAID", "Bill Paid"
        BILL_DELETED = "BILL_DELETED", "Bill Deleted"
        PAYMENT_RECORDED = "PAYMENT_RECORDED", "Payment Recorded"
        PAYMENT_ADJUSTED = "PAYMENT_ADJUSTED", "Payment Adjusted"
        PAYMENT_REVERSED = "PAYMENT_REVERSED", "Payment Reversed"
        MEMBER_INVITED = "MEMBER_INVITED", "Member Invited"
        MEMBER_ADDED = "MEMBER_ADDED", "Member Added"
        MEMBER_REMOVED = "MEMBER_REMOVED", "Member Removed"
        ROLE_CHANGED = "ROLE_CHANGED", "Role Changed"
        DOCUMENT_UPLOADED = "DOCUMENT_UPLOADED", "Document Uploaded"
        DOCUMENT_DELETED = "DOCUMENT_DELETED", "Document Deleted"
        EXTRACTION_COMPLETED = "EXTRACTION_COMPLETED", "Document Extraction Completed"
        EXTRACTION_FAILED = "EXTRACTION_FAILED", "Document Extraction Failed"
        RECURRING_CHANGED = "RECURRING_CHANGED", "Recurring Schedule Changed"
        REMINDER_CREATED = "REMINDER_CREATED", "Reminder Created"
        NOTIFICATION_DELIVERED = "NOTIFICATION_DELIVERED", "Notification Delivered"
        AI_TOOL_EXECUTED = "AI_TOOL_EXECUTED", "AI Tool Executed"
        SECURITY_ALERT = "SECURITY_ALERT", "Security Alert"

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_events",
        help_text="User who performed this action. Preserved if user is deleted.",
    )
    household = models.ForeignKey(
        "households.Household",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="audit_events",
        help_text="Tenant boundary for this audit event.",
    )
    action = models.CharField(
        max_length=50,
        choices=Action.choices,
        db_index=True,
    )
    entity_type = models.CharField(
        max_length=50,
        blank=True,
        help_text="Model name of the affected entity (e.g. Bill, Payment).",
    )
    entity_id = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Primary key of the affected entity.",
    )
    description = models.TextField(help_text="Human-readable summary of the event.")
    metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text="Structured before/after state or event parameters. Never store passwords or secrets.",
    )
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "homepilot_audit_events"
        verbose_name = "Audit Event"
        verbose_name_plural = "Audit Events"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["household", "created_at"]),
            models.Index(fields=["action", "created_at"]),
        ]

    def __str__(self):
        actor_name = getattr(self.actor, "username", "System")
        return f"[{self.created_at.strftime('%Y-%m-%d %H:%M')}] {self.action} by {actor_name}"
