from rest_framework import viewsets, status, generics
from rest_framework.permissions import IsAuthenticated
from rest_framework.decorators import action
from rest_framework.response import Response

from .models import BillReminder, NotificationLog
from .serializers import BillReminderSerializer, NotificationLogSerializer
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
