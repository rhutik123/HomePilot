import logging
from decimal import Decimal
from datetime import timedelta
from typing import List
from django.db.models import Avg, Sum, Count
from django.utils import timezone

from apps.bills.models import Bill, BillCategory, BillStatus
from apps.ai.models import SpendingInsight
from apps.households.models import Household

logger = logging.getLogger(__name__)


class SpendingAnomalyDetector:
    """
    Automated analytical engine that inspects household spending history,
    detects expense spikes, moving average deviations, and upcoming forecasts,
    and creates actionable SpendingInsight records.
    """
    @classmethod
    def analyze_household(cls, household: Household) -> List[SpendingInsight]:
        today = timezone.localdate()
        six_months_ago = today - timedelta(days=180)
        thirty_days_ago = today - timedelta(days=30)

        created_insights = []

        # 1. Detect Category Expense Spikes (recent bills vs historical average)
        historical_bills = Bill.objects.filter(
            household=household,
            due_date__gte=six_months_ago,
            due_date__lt=thirty_days_ago,
        )

        recent_bills = Bill.objects.filter(
            household=household,
            due_date__gte=thirty_days_ago,
        )

        # Calculate category averages historically
        cat_averages = (
            historical_bills.values("category")
            .annotate(avg_amount=Avg("amount"), count=Count("id"))
        )
        avg_map = {row["category"]: row["avg_amount"] for row in cat_averages if row["count"] >= 2}

        for recent_bill in recent_bills:
            cat = recent_bill.category
            if cat in avg_map:
                hist_avg = avg_map[cat]
                threshold = hist_avg * Decimal("1.35")  # 35% spike
                diff = recent_bill.amount - hist_avg

                if recent_bill.amount > threshold and diff >= Decimal("200.00"):
                    # Check if an active insight already exists
                    existing = SpendingInsight.objects.filter(
                        household=household,
                        insight_type=SpendingInsight.InsightType.EXPENSE_SPIKE,
                        category=cat,
                        is_dismissed=False,
                    ).exists()

                    if not existing:
                        severity = SpendingInsight.Severity.CRITICAL if recent_bill.amount > (hist_avg * Decimal("1.70")) else SpendingInsight.Severity.WARNING
                        insight = SpendingInsight.objects.create(
                            household=household,
                            insight_type=SpendingInsight.InsightType.EXPENSE_SPIKE,
                            title=f"Unusual {recent_bill.get_category_display()} Bill from {recent_bill.provider_name}",
                            description=(
                                f"The recent {recent_bill.get_category_display()} bill of {recent_bill.amount} {recent_bill.currency} "
                                f"is {diff:.2f} {recent_bill.currency} (+{(diff / hist_avg * 100):.0f}%) higher than your "
                                f"historical average of {hist_avg:.2f} {recent_bill.currency}."
                            ),
                            severity=severity,
                            category=cat,
                            amount_difference=diff,
                        )
                        created_insights.append(insight)

        # 2. Upcoming 30-Day Expense Forecast
        next_month_due = Bill.objects.filter(
            household=household,
            due_date__gte=today,
            due_date__lte=today + timedelta(days=30),
        ).exclude(status=BillStatus.PAID)

        upcoming_total = next_month_due.aggregate(sum=Sum("outstanding_balance"))["sum"] or Decimal("0.00")
        if upcoming_total > Decimal("0.00"):
            forecast_title = f"Upcoming 30-Day Household Expenses ({today.strftime('%B %Y')})"
            existing_forecast = SpendingInsight.objects.filter(
                household=household,
                insight_type=SpendingInsight.InsightType.UPCOMING_EXPENSE_FORECAST,
                title=forecast_title,
                is_dismissed=False,
            ).exists()

            if not existing_forecast:
                insight = SpendingInsight.objects.create(
                    household=household,
                    insight_type=SpendingInsight.InsightType.UPCOMING_EXPENSE_FORECAST,
                    title=forecast_title,
                    description=(
                        f"You have {next_month_due.count()} bills due in the next 30 days totaling "
                        f"{upcoming_total:.2f} INR across your household."
                    ),
                    severity=SpendingInsight.Severity.INFO,
                    amount_difference=upcoming_total,
                )
                created_insights.append(insight)

        return created_insights
