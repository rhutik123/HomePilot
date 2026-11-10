import logging
import calendar
from datetime import date, timedelta
from decimal import Decimal
from typing import Dict, Any, List, Optional
from django.db.models import Sum, Avg, Count, Q
from django.utils import timezone

from apps.bills.models import Bill, BillStatus, BillCategory
from apps.payments.models import Payment, PaymentMethod, PaymentStatus
from apps.households.models import Household
from apps.recurring.models import RecurringSchedule, RecurrenceFrequency

logger = logging.getLogger(__name__)


class AnalyticsService:
    """
    High-performance financial analytics and aggregation engine.
    Computes household KPIs, category breakdowns, provider analysis, and spending trends.
    """
    @classmethod
    def get_overview_metrics(
        cls,
        household: Household,
        date_from: Optional[date] = None,
        date_to: Optional[date] = None,
    ) -> Dict[str, Any]:
        queryset = Bill.objects.filter(household=household)
        if date_from:
            queryset = queryset.filter(due_date__gte=date_from)
        if date_to:
            queryset = queryset.filter(due_date__lte=date_to)

        aggregates = queryset.aggregate(
            total_billed=Sum("amount"),
            total_paid=Sum("total_paid"),
            total_outstanding=Sum("outstanding_balance"),
            avg_bill=Avg("amount"),
            total_count=Count("id"),
            paid_count=Count("id", filter=Q(status=BillStatus.PAID)),
            partially_paid_count=Count("id", filter=Q(status=BillStatus.PARTIALLY_PAID)),
            pending_count=Count("id", filter=Q(status=BillStatus.PENDING)),
            overdue_count=Count("id", filter=Q(status=BillStatus.OVERDUE)),
            overdue_amount=Sum("outstanding_balance", filter=Q(status=BillStatus.OVERDUE)),
        )

        total_billed = aggregates["total_billed"] or Decimal("0.00")
        total_paid = aggregates["total_paid"] or Decimal("0.00")
        total_outstanding = aggregates["total_outstanding"] or Decimal("0.00")
        overdue_amount = aggregates["overdue_amount"] or Decimal("0.00")
        avg_bill = aggregates["avg_bill"] or Decimal("0.00")

        settlement_rate = Decimal("0.0")
        if total_billed > Decimal("0.00"):
            settlement_rate = round((total_paid / total_billed) * Decimal("100.0"), 2)

        # Payment records count in range
        payment_qs = Payment.objects.filter(household=household)
        if date_from:
            payment_qs = payment_qs.filter(payment_date__gte=date_from)
        if date_to:
            payment_qs = payment_qs.filter(payment_date__lte=date_to)

        payment_sum = payment_qs.aggregate(total=Sum("amount"), count=Count("id"))

        return {
            "household_id": household.id,
            "household_name": household.name,
            "date_from": date_from.isoformat() if date_from else None,
            "date_to": date_to.isoformat() if date_to else None,
            "total_billed": str(total_billed),
            "total_paid": str(total_paid),
            "total_outstanding": str(total_outstanding),
            "settlement_rate_pct": float(settlement_rate),
            "avg_bill_amount": str(round(avg_bill, 2)),
            "bill_counts": {
                "total": aggregates["total_count"],
                "paid": aggregates["paid_count"],
                "partially_paid": aggregates["partially_paid_count"],
                "pending": aggregates["pending_count"],
                "overdue": aggregates["overdue_count"],
            },
            "overdue_amount": str(overdue_amount),
            "payments_recorded": {
                "count": payment_sum["count"],
                "total_amount": str(payment_sum["total"] or Decimal("0.00")),
            },
        }

    @classmethod
    def get_category_metrics(
        cls,
        household: Household,
        date_from: Optional[date] = None,
        date_to: Optional[date] = None,
    ) -> List[Dict[str, Any]]:
        queryset = Bill.objects.filter(household=household)
        if date_from:
            queryset = queryset.filter(due_date__gte=date_from)
        if date_to:
            queryset = queryset.filter(due_date__lte=date_to)

        grand_total = queryset.aggregate(sum=Sum("amount"))["sum"] or Decimal("0.00")

        grouped = (
            queryset.values("category")
            .annotate(
                total_amount=Sum("amount"),
                total_paid=Sum("total_paid"),
                outstanding=Sum("outstanding_balance"),
                bill_count=Count("id"),
            )
            .order_by("-total_amount")
        )

        results = []
        for row in grouped:
            cat_total = row["total_amount"] or Decimal("0.00")
            pct = Decimal("0.0")
            if grand_total > Decimal("0.00"):
                pct = round((cat_total / grand_total) * Decimal("100.0"), 2)

            cat_choices = dict(BillCategory.choices)
            results.append({
                "category": row["category"],
                "category_display": cat_choices.get(row["category"], row["category"]),
                "total_amount": str(cat_total),
                "total_paid": str(row["total_paid"] or Decimal("0.00")),
                "outstanding_balance": str(row["outstanding"] or Decimal("0.00")),
                "bill_count": row["bill_count"],
                "percentage_of_total": float(pct),
            })

        return results

    @classmethod
    def get_provider_metrics(
        cls,
        household: Household,
        date_from: Optional[date] = None,
        date_to: Optional[date] = None,
    ) -> List[Dict[str, Any]]:
        queryset = Bill.objects.filter(household=household)
        if date_from:
            queryset = queryset.filter(due_date__gte=date_from)
        if date_to:
            queryset = queryset.filter(due_date__lte=date_to)

        grouped = (
            queryset.values("provider_name")
            .annotate(
                total_amount=Sum("amount"),
                total_paid=Sum("total_paid"),
                outstanding=Sum("outstanding_balance"),
                bill_count=Count("id"),
            )
            .order_by("-total_amount")
        )

        results = []
        for row in grouped:
            results.append({
                "provider_name": row["provider_name"],
                "total_amount": str(row["total_amount"] or Decimal("0.00")),
                "total_paid": str(row["total_paid"] or Decimal("0.00")),
                "outstanding_balance": str(row["outstanding"] or Decimal("0.00")),
                "bill_count": row["bill_count"],
            })

        return results

    @classmethod
    def get_spending_trends(
        cls,
        household: Household,
        months: int = 6,
    ) -> List[Dict[str, Any]]:
        """
        Calculates monthly spending history and month-over-month percentage changes.
        """
        today = timezone.localdate()
        trends = []
        prev_month_billed = None

        # Build list of (year, month) for past N months ascending
        month_keys = []
        for i in range(months - 1, -1, -1):
            # approximate month deduction
            d = today.replace(day=1) - timedelta(days=i * 28)
            d = d.replace(day=1)
            month_keys.append((d.year, d.month))

        # Deduplicate while preserving order
        unique_keys = []
        for k in month_keys:
            if k not in unique_keys:
                unique_keys.append(k)

        for year, month in unique_keys:
            month_bills = Bill.objects.filter(
                household=household,
                due_date__year=year,
                due_date__month=month,
            )
            agg = month_bills.aggregate(
                total_billed=Sum("amount"),
                total_paid=Sum("total_paid"),
                bill_count=Count("id"),
            )

            billed = agg["total_billed"] or Decimal("0.00")
            paid = agg["total_paid"] or Decimal("0.00")

            mom_change = None
            if prev_month_billed is not None:
                if prev_month_billed > Decimal("0.00"):
                    diff = billed - prev_month_billed
                    mom_change = float(round((diff / prev_month_billed) * Decimal("100.0"), 2))
                elif billed > Decimal("0.00"):
                    mom_change = 100.0
                else:
                    mom_change = 0.0

            month_name = calendar.month_name[month]
            trends.append({
                "period": f"{year}-{month:02d}",
                "label": f"{month_name} {year}",
                "total_billed": str(billed),
                "total_paid": str(paid),
                "bill_count": agg["bill_count"],
                "mom_change_pct": mom_change,
            })
            prev_month_billed = billed

        return trends

    @classmethod
    def get_recurring_metrics(cls, household: Household) -> Dict[str, Any]:
        """
        Calculates commitments from active recurring bill schedules.
        """
        active_schedules = (
            RecurringSchedule.objects.filter(household=household, is_active=True)
            .select_related("assigned_to")
            .order_by("next_due_date")
        )

        monthly_commitment = Decimal("0.00")
        schedules_list = []

        for sched in active_schedules:
            amt = sched.expected_amount or Decimal("0.00")
            monthly_equiv = amt

            if sched.frequency == RecurrenceFrequency.MONTHLY:
                monthly_equiv = amt
            elif sched.frequency == RecurrenceFrequency.QUARTERLY:
                monthly_equiv = (amt / Decimal("3")).quantize(Decimal("0.01"))
            elif sched.frequency == RecurrenceFrequency.YEARLY:
                monthly_equiv = (amt / Decimal("12")).quantize(Decimal("0.01"))

            monthly_commitment += monthly_equiv
            schedules_list.append({
                "id": sched.id,
                "provider_name": sched.provider_name,
                "category": sched.category,
                "category_display": sched.get_category_display(),
                "frequency": sched.frequency,
                "frequency_display": sched.get_frequency_display(),
                "expected_amount": f"{amt:.2f}",
                "monthly_equivalent": f"{monthly_equiv:.2f}",
                "next_due_date": sched.next_due_date.isoformat() if sched.next_due_date else None,
                "assigned_to": getattr(sched.assigned_to, "display_name", "Unassigned"),
            })

        return {
            "active_schedules_count": active_schedules.count(),
            "estimated_monthly_recurring_total": f"{monthly_commitment:.2f}",
            "schedules": schedules_list,
        }

    @classmethod
    def get_obligations_metrics(cls, household: Household) -> Dict[str, Any]:
        """
        Calculates upcoming and overdue financial obligations.
        """
        today = timezone.localdate()
        next_week = today + timedelta(days=7)
        next_month = today + timedelta(days=30)

        unpaid = Bill.objects.filter(
            household=household,
            status__in=[BillStatus.PENDING, BillStatus.PARTIALLY_PAID, BillStatus.OVERDUE],
        )

        total_outstanding = unpaid.aggregate(sum=Sum("outstanding_balance"))["sum"] or Decimal("0.00")

        # Overdue
        overdue_bills = unpaid.filter(due_date__lt=today).order_by("due_date")
        overdue_total = overdue_bills.aggregate(sum=Sum("outstanding_balance"))["sum"] or Decimal("0.00")

        # Due next 7 days
        due_next_week = unpaid.filter(due_date__gte=today, due_date__lte=next_week).order_by("due_date")
        due_next_week_total = due_next_week.aggregate(sum=Sum("outstanding_balance"))["sum"] or Decimal("0.00")

        # Due next 30 days
        due_next_month = unpaid.filter(due_date__gte=today, due_date__lte=next_month).order_by("due_date")
        due_next_month_total = due_next_month.aggregate(sum=Sum("outstanding_balance"))["sum"] or Decimal("0.00")

        return {
            "total_outstanding_amount": f"{total_outstanding:.2f}",
            "overdue_count": overdue_bills.count(),
            "overdue_total": f"{overdue_total:.2f}",
            "due_next_week_count": due_next_week.count(),
            "due_next_week_total": f"{due_next_week_total:.2f}",
            "due_next_month_count": due_next_month.count(),
            "due_next_month_total": f"{due_next_month_total:.2f}",
        }

    @classmethod
    def get_payments_metrics(
        cls,
        household: Household,
        date_from: Optional[date] = None,
        date_to: Optional[date] = None,
        limit: int = 50,
    ) -> Dict[str, Any]:
        """
        Calculates payment history metrics and recent transactions.
        """
        payments_qs = (
            Payment.objects.filter(household=household)
            .select_related("bill", "paid_by")
            .order_by("-payment_date", "-id")
        )

        if date_from:
            payments_qs = payments_qs.filter(payment_date__gte=date_from)
        if date_to:
            payments_qs = payments_qs.filter(payment_date__lte=date_to)

        total_settled = payments_qs.aggregate(sum=Sum("amount"))["sum"] or Decimal("0.00")

        # Method breakdown
        methods_agg = payments_qs.values("payment_method").annotate(total=Sum("amount"), count=Count("id"))

        payments_list = []
        for p in payments_qs[:limit]:
            has_receipt = False
            try:
                has_receipt = hasattr(p, "receipt") and p.receipt is not None
            except Exception:
                has_receipt = False

            payments_list.append({
                "id": p.id,
                "bill_id": p.bill.id if p.bill else None,
                "provider_name": p.bill.provider_name if p.bill else "Direct",
                "amount": f"{p.amount:.2f}",
                "payment_date": p.payment_date.isoformat(),
                "payment_method": p.payment_method,
                "transaction_reference": p.transaction_reference or "N/A",
                "paid_by": getattr(p.paid_by, "display_name", "Unknown"),
                "has_receipt": has_receipt,
            })

        return {
            "total_payments_count": payments_qs.count(),
            "total_amount_settled": f"{total_settled:.2f}",
            "methods_breakdown": [
                {
                    "method": m["payment_method"],
                    "total": f"{m['total']:.2f}",
                    "count": m["count"],
                }
                for m in methods_agg
            ],
            "recent_payments": payments_list,
        }
