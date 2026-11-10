from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated

from .models import RecurringSchedule, RecurringOccurrence
from .serializers import RecurringScheduleSerializer, RecurringOccurrenceSerializer
from .services.schedule_service import RecurringScheduleService, calculate_next_due_date, get_period_identifier
from apps.bills.models import Bill
from apps.reminders.services.reminder_service import ReminderService


class RecurringScheduleViewSet(viewsets.ModelViewSet):
    """
    CRUD API for household recurring bill schedules.
    Enforces multi-tenant isolation and provides pause/resume/trigger actions.
    """
    permission_classes = [IsAuthenticated]
    serializer_class = RecurringScheduleSerializer

    def get_queryset(self):
        queryset = (
            RecurringSchedule.objects.filter(household__memberships__user=self.request.user)
            .select_related("household", "created_by", "assigned_to")
            .prefetch_related("occurrences__bill")
            .distinct()
        )

        household_id = self.request.query_params.get("household")
        if household_id:
            queryset = queryset.filter(household_id=household_id)

        is_active = self.request.query_params.get("is_active")
        if is_active is not None:
            queryset = queryset.filter(is_active=is_active.lower() in ("true", "1"))

        return queryset

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)

    @action(detail=True, methods=["post"])
    def pause(self, request, pk=None):
        """Pauses a recurring schedule to halt automatic bill generation."""
        schedule = self.get_object()
        schedule.is_active = False
        schedule.save(update_fields=["is_active", "updated_at"])
        return Response(
            {"message": f"Recurring schedule for '{schedule.provider_name}' paused.", "is_active": False},
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"])
    def resume(self, request, pk=None):
        """Resumes a paused recurring schedule."""
        schedule = self.get_object()
        schedule.is_active = True
        schedule.save(update_fields=["is_active", "updated_at"])
        return Response(
            {"message": f"Recurring schedule for '{schedule.provider_name}' resumed.", "is_active": True},
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"])
    def trigger(self, request, pk=None):
        """On-demand execution: generates the next bill occurrence immediately."""
        schedule = self.get_object()
        period_id = get_period_identifier(schedule.next_due_date, schedule.frequency)

        if RecurringOccurrence.objects.filter(schedule=schedule, period_identifier=period_id).exists():
            return Response(
                {"error": f"Occurrence for period '{period_id}' has already been generated."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        bill = Bill.objects.create(
            household=schedule.household,
            uploaded_by=request.user,
            assigned_to=schedule.assigned_to,
            provider_name=schedule.provider_name,
            category=schedule.category,
            amount=schedule.expected_amount,
            currency=schedule.currency,
            billing_period=f"{schedule.next_due_date.strftime('%B %Y')} ({schedule.get_frequency_display()})",
            issue_date=request.data.get("issue_date") or schedule.next_due_date,
            due_date=schedule.next_due_date,
            notes=f"Manually triggered from recurring schedule #{schedule.id}.",
        )

        occurrence = RecurringOccurrence.objects.create(
            schedule=schedule,
            bill=bill,
            period_identifier=period_id,
        )

        ReminderService.create_reminders_for_bill(bill)

        # Advance next due date
        schedule.next_due_date = calculate_next_due_date(
            schedule.next_due_date,
            schedule.frequency,
            schedule.interval_count,
            schedule.day_of_month,
        )
        schedule.save(update_fields=["next_due_date", "updated_at"])

        return Response(
            {
                "message": f"Generated bill occurrence for {schedule.provider_name}.",
                "occurrence": RecurringOccurrenceSerializer(occurrence).data,
                "next_due_date": schedule.next_due_date.isoformat(),
            },
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["get"])
    def occurrences(self, request, pk=None):
        """Lists all historical bill occurrences generated for this schedule."""
        schedule = self.get_object()
        occurrences = schedule.occurrences.select_related("bill").all()
        serializer = RecurringOccurrenceSerializer(occurrences, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)
