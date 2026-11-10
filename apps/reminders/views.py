from rest_framework import viewsets, status, generics
from rest_framework.permissions import IsAuthenticated
from rest_framework.decorators import action
from rest_framework.response import Response

from .models import (
    BillReminder,
    NotificationLog,
    NotificationPreference,
    InAppNotification,
    NotificationDelivery,
)
from .serializers import (
    BillReminderSerializer,
    NotificationLogSerializer,
    NotificationPreferenceSerializer,
    InAppNotificationSerializer,
    NotificationDeliverySerializer,
)
from .tasks import send_bill_reminder_task


class BillReminderViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Read-only ViewSet for inspecting scheduled reminders for the user's households.
    Includes on-demand test dispatch action.
    """
    permission_classes = [IsAuthenticated]
    serializer_class = BillReminderSerializer

    def get_queryset(self):
        return (
            BillReminder.objects.filter(bill__household__memberships__user=self.request.user)
            .select_related("bill", "sent_to")
            .distinct()
        )

    @action(detail=True, methods=["post"])
    def trigger(self, request, pk=None):
        """On-demand trigger to execute a specific reminder immediately."""
        reminder = self.get_object()
        send_bill_reminder_task(reminder.id)
        reminder.refresh_from_db()
        return Response(
            {
                "message": f"Reminder {reminder.id} executed.",
                "reminder": BillReminderSerializer(reminder).data,
            },
            status=status.HTTP_200_OK,
        )


class NotificationPreferenceView(generics.RetrieveUpdateAPIView):
    """
    Endpoint for inspecting and customizing user notification channel preferences.
    """
    permission_classes = [IsAuthenticated]
    serializer_class = NotificationPreferenceSerializer

    def get_object(self):
        prefs, _ = NotificationPreference.objects.get_or_create(
            user=self.request.user,
            defaults={
                "email_enabled": True,
                "in_app_enabled": True,
                "whatsapp_enabled": False,
            },
        )
        return prefs


class InAppNotificationViewSet(viewsets.ModelViewSet):
    """
    User in-app notifications inbox.
    Supports listing, filtering by unread, and marking notifications as read.
    """
    permission_classes = [IsAuthenticated]
    serializer_class = InAppNotificationSerializer
    http_method_names = ["get", "post", "patch", "head", "options"]

    def get_queryset(self):
        queryset = InAppNotification.objects.filter(user=self.request.user).select_related("bill", "reminder")
        is_read = self.request.query_params.get("is_read")
        if is_read is not None:
            queryset = queryset.filter(is_read=is_read.lower() in ("true", "1"))
        return queryset

    @action(detail=False, methods=["post"])
    def mark_all_read(self, request):
        """Marks all unread in-app alerts as read for the authenticated user."""
        count = InAppNotification.objects.filter(user=request.user, is_read=False).update(is_read=True)
        return Response({"message": f"Marked {count} notifications as read.", "count": count}, status=status.HTTP_200_OK)


class NotificationDeliveryListView(generics.ListAPIView):
    """
    Endpoint for auditing multi-channel delivery attempts across the user's households.
    """
    permission_classes = [IsAuthenticated]
    serializer_class = NotificationDeliverySerializer

    def get_queryset(self):
        return (
            NotificationDelivery.objects.filter(bill__household__memberships__user=self.request.user)
            .select_related("bill", "reminder", "recipient")
            .distinct()
        )


class NotificationLogListView(generics.ListAPIView):
    """
    Endpoint for viewing notification delivery logs for the user's households.
    """
    permission_classes = [IsAuthenticated]
    serializer_class = NotificationLogSerializer

    def get_queryset(self):
        return (
            NotificationLog.objects.filter(bill__household__memberships__user=self.request.user)
            .select_related("bill", "reminder")
            .distinct()
        )
