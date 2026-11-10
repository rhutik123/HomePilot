from django.db.models import Sum, Q
from django.utils import timezone
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated

from apps.bills.models import Bill, BillStatus
from apps.bills.serializers import BillSerializer


class DashboardSummaryAPIView(APIView):
    """
    Returns aggregated metrics and bill slices for the logged-in user's households:
    - Total pending bills and amount
    - Overdue bills and amount
    - Paid bills and amount
    - Bills assigned to the current user
    - Quick lists (upcoming, overdue, recently paid, assigned to me)
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        today = timezone.localdate()

        # Base queryset strictly scoped to user's households
        user_bills = Bill.objects.filter(
            household__memberships__user=user
        ).select_related("household", "assigned_to", "uploaded_by").distinct()

        # 1. Pending (future or due today)
        pending_qs = user_bills.filter(status=BillStatus.PENDING, due_date__gte=today)
        pending_count = pending_qs.count()
        pending_total = pending_qs.aggregate(total=Sum("amount"))["total"] or 0

        # 2. Overdue (due_date < today and not paid, or status=OVERDUE)
        overdue_qs = user_bills.filter(
            Q(status=BillStatus.OVERDUE) | (Q(status=BillStatus.PENDING) & Q(due_date__lt=today))
        )
        overdue_count = overdue_qs.count()
        overdue_total = overdue_qs.aggregate(total=Sum("amount"))["total"] or 0

        # 3. Paid
        paid_qs = user_bills.filter(status=BillStatus.PAID)
        paid_count = paid_qs.count()
        paid_total = paid_qs.aggregate(total=Sum("amount"))["total"] or 0

        # 4. Assigned to logged-in user (pending only)
        assigned_qs = user_bills.filter(assigned_to=user).exclude(status=BillStatus.PAID)
        assigned_count = assigned_qs.count()

        # 5. Quick Slices
        upcoming_bills = pending_qs.order_by("due_date")[:5]
        overdue_bills = overdue_qs.order_by("due_date")[:5]
        recent_paid = paid_qs.order_by("-paid_at", "-updated_at")[:5]
        assigned_to_me = assigned_qs.order_by("due_date")[:5]

        return Response({
            "metrics": {
                "pending_count": pending_count,
                "pending_total": f"{pending_total:.2f}",
                "overdue_count": overdue_count,
                "overdue_total": f"{overdue_total:.2f}",
                "paid_count": paid_count,
                "paid_total": f"{paid_total:.2f}",
                "assigned_to_me_count": assigned_count,
            },
            "upcoming_bills": BillSerializer(upcoming_bills, many=True).data,
            "overdue_bills": BillSerializer(overdue_bills, many=True).data,
            "recently_paid_bills": BillSerializer(recent_paid, many=True).data,
            "assigned_to_me_bills": BillSerializer(assigned_to_me, many=True).data,
        })
