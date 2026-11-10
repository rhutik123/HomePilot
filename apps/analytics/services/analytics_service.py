import logging
import calendar
from datetime import date, timedelta
from decimal import Decimal
from typing import Dict, Any, List, Optional
from django.db.models import Sum, Avg, Count, Q
from django.utils import timezone

from apps.bills.models import Bill, BillStatus, BillCategory
from apps.payments.models import Payment
from apps.households.models import Household

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
