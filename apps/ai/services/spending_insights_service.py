import logging
from decimal import Decimal
from datetime import timedelta
from typing import Dict, Any, List
from django.db.models import Sum, Count, Avg, Max, Q
from django.utils import timezone
from django.conf import settings

from apps.bills.models import Bill, BillCategory, BillStatus
from apps.payments.models import Payment
from apps.recurring.models import RecurringSchedule, RecurrenceFrequency
from apps.households.models import Household
from apps.ai.models import SpendingInsight, AIUsageRecord

logger = logging.getLogger(__name__)


class SpendingInsightsService:
    """
    Core AI Spending Insights Engine.
    ARCHITECTURAL MANDATE:
    Django and PostgreSQL calculate all financial figures deterministically.
    AI (LLM) only formats, contextualizes, and explains the calculated facts,
    and is strictly prohibited from inventing numbers.
    """

    @staticmethod
    def _format_currency(val) -> str:
        if val is None:
            return "0.00"
        return f"{Decimal(str(val)):.2f}"

    @classmethod
    def get_monthly_comparison(cls, household: Household) -> Dict[str, Any]:
        """
        Calculates month-over-month total spending, comparing the current
        calendar month against the preceding calendar month.
        """
        today = timezone.localdate()
        current_year = today.year
        current_month = today.month

        if current_month == 1:
            prev_year = current_year - 1
            prev_month = 12
        else:
            prev_year = current_year
            prev_month = current_month - 1

        curr_bills = Bill.objects.filter(
            household=household,
            due_date__year=current_year,
            due_date__month=current_month,
        )
        prev_bills = Bill.objects.filter(
            household=household,
            due_date__year=prev_year,
            due_date__month=prev_month,
        )

        curr_total = curr_bills.aggregate(sum=Sum("amount"))["sum"] or Decimal("0.00")
        curr_paid = curr_bills.aggregate(sum=Sum("total_paid"))["sum"] or Decimal("0.00")
        curr_outstanding = curr_bills.aggregate(sum=Sum("outstanding_balance"))["sum"] or Decimal("0.00")

        prev_total = prev_bills.aggregate(sum=Sum("amount"))["sum"] or Decimal("0.00")
        prev_paid = prev_bills.aggregate(sum=Sum("total_paid"))["sum"] or Decimal("0.00")

        difference = curr_total - prev_total
        pct_change = Decimal("0.0")
        if prev_total > Decimal("0.00"):
            pct_change = ((difference / prev_total) * Decimal("100.0")).quantize(Decimal("0.1"))

        direction = "UNCHANGED"
        if difference > Decimal("0.00"):
            direction = "INCREASED"
        elif difference < Decimal("0.00"):
            direction = "DECREASED"

        return {
            "current_month": f"{current_year}-{current_month:02d}",
            "current_total_billed": cls._format_currency(curr_total),
            "current_total_paid": cls._format_currency(curr_paid),
            "current_outstanding": cls._format_currency(curr_outstanding),
            "current_bills_count": curr_bills.count(),
            "previous_month": f"{prev_year}-{prev_month:02d}",
            "previous_total_billed": cls._format_currency(prev_total),
            "previous_total_paid": cls._format_currency(prev_paid),
            "difference": cls._format_currency(difference),
            "percentage_change": f"{pct_change:+.1f}%",
            "direction": direction,
        }

    @classmethod
    def get_category_breakdown(cls, household: Household, months: int = 6) -> Dict[str, Any]:
        """
        Aggregates spending across all categories for the past N months.
        Identifies the top spending category and its proportion of total spend.
        """
        today = timezone.localdate()
        start_date = today - timedelta(days=int(months) * 30)

        bills = Bill.objects.filter(
            household=household,
            due_date__gte=start_date,
        )

        grand_total = bills.aggregate(sum=Sum("amount"))["sum"] or Decimal("0.00")

        cat_summary = (
            bills.values("category")
            .annotate(
                total=Sum("amount"),
                count=Count("id"),
                avg=Avg("amount"),
            )
            .order_by("-total")
        )

        categories = []
        top_category = None
        for row in cat_summary:
            total_amt = row["total"] or Decimal("0.00")
            pct = "0.0%"
            if grand_total > Decimal("0.00"):
                pct = f"{((total_amt / grand_total) * 100):.1f}%"

            cat_info = {
                "category": row["category"],
                "category_display": dict(BillCategory.choices).get(row["category"], row["category"]),
                "total_amount": cls._format_currency(total_amt),
                "bills_count": row["count"],
                "average_amount": cls._format_currency(row["avg"]),
                "percentage_of_total": pct,
            }
            categories.append(cat_info)
            if not top_category:
                top_category = cat_info

        return {
            "period_months": months,
            "grand_total": cls._format_currency(grand_total),
            "top_category": top_category,
            "categories": categories,
        }

    @classmethod
    def get_category_history(cls, household: Household, category: str, months: int = 6) -> Dict[str, Any]:
        """
        Calculates month-by-month historical spend for a specific utility category
        (e.g., Electricity in the last six months).
        """
        today = timezone.localdate()
        start_date = today - timedelta(days=int(months) * 30)

        bills = (
            Bill.objects.filter(
                household=household,
                category=category,
                due_date__gte=start_date,
            )
            .order_by("due_date")
        )

        total_spent = bills.aggregate(sum=Sum("amount"))["sum"] or Decimal("0.00")
        avg_spent = bills.aggregate(avg=Avg("amount"))["avg"] or Decimal("0.00")

        monthly_records = []
        for b in bills:
            monthly_records.append({
                "bill_id": b.id,
                "provider_name": b.provider_name,
                "amount": cls._format_currency(b.amount),
                "due_date": b.due_date.isoformat(),
                "status": b.status,
            })

        return {
            "category": category,
            "category_display": dict(BillCategory.choices).get(category, category),
            "period_months": months,
            "total_spent": cls._format_currency(total_spent),
            "average_spent": cls._format_currency(avg_spent),
            "bills_count": len(monthly_records),
            "records": monthly_records,
        }

    @classmethod
    def get_increasing_bills_and_rate_hikes(cls, household: Household) -> List[Dict[str, Any]]:
        """
        Detects providers whose latest charges have increased relative to the
        immediately preceding bill or historical baseline.
        """
        providers = (
            Bill.objects.filter(household=household)
            .order_by()
            .values_list("provider_name", flat=True)
            .distinct()
        )

        hikes = []
        for provider in providers:
            recent_bills = list(
                Bill.objects.filter(household=household, provider_name=provider)
                .order_by("-due_date")[:2]
            )
            if len(recent_bills) >= 2:
                latest = recent_bills[0]
                prev = recent_bills[1]
                diff = latest.amount - prev.amount
                if diff > Decimal("0.00") and prev.amount > Decimal("0.00"):
                    pct = ((diff / prev.amount) * Decimal("100.0")).quantize(Decimal("0.1"))
                    if pct >= Decimal("5.0"):  # At least 5% increase
                        hikes.append({
                            "provider_name": provider,
                            "category": latest.category,
                            "category_display": latest.get_category_display(),
                            "latest_amount": cls._format_currency(latest.amount),
                            "latest_due_date": latest.due_date.isoformat(),
                            "previous_amount": cls._format_currency(prev.amount),
                            "previous_due_date": prev.due_date.isoformat(),
                            "increase_amount": cls._format_currency(diff),
                            "percentage_increase": f"+{pct:.1f}%",
                        })

        return sorted(hikes, key=lambda x: Decimal(x["increase_amount"]), reverse=True)

    @classmethod
    def get_upcoming_obligations(cls, household: Household) -> Dict[str, Any]:
        """
        Calculates upcoming payment obligations:
        - Due within next 7 days (next week)
        - Due within next 30 days (next month)
        - Currently overdue bills
        - Total outstanding debt
        """
        today = timezone.localdate()
        next_week = today + timedelta(days=7)
        next_month = today + timedelta(days=30)

        unpaid_bills = Bill.objects.filter(
            household=household,
            status__in=[BillStatus.PENDING, BillStatus.PARTIALLY_PAID, BillStatus.OVERDUE],
        )
        total_outstanding = unpaid_bills.aggregate(sum=Sum("outstanding_balance"))["sum"] or Decimal("0.00")

        # Due next week (0 to 7 days ahead)
        due_next_week = unpaid_bills.filter(due_date__gte=today, due_date__lte=next_week).order_by("due_date")
        next_week_total = due_next_week.aggregate(sum=Sum("outstanding_balance"))["sum"] or Decimal("0.00")

        # Due next 30 days
        due_next_month = unpaid_bills.filter(due_date__gte=today, due_date__lte=next_month).order_by("due_date")
        next_month_total = due_next_month.aggregate(sum=Sum("outstanding_balance"))["sum"] or Decimal("0.00")

        # Overdue
        overdue_bills = unpaid_bills.filter(due_date__lt=today).order_by("due_date")
        overdue_total = overdue_bills.aggregate(sum=Sum("outstanding_balance"))["sum"] or Decimal("0.00")

        return {
            "total_outstanding_amount": cls._format_currency(total_outstanding),
            "overdue_count": overdue_bills.count(),
            "overdue_total": cls._format_currency(overdue_total),
            "due_next_week_count": due_next_week.count(),
            "due_next_week_total": cls._format_currency(next_week_total),
            "due_next_week_bills": [
                {
                    "id": b.id,
                    "provider_name": b.provider_name,
                    "outstanding_balance": cls._format_currency(b.outstanding_balance),
                    "due_date": b.due_date.isoformat(),
                    "category": b.category,
                }
                for b in due_next_week
            ],
            "due_next_month_count": due_next_month.count(),
            "due_next_month_total": cls._format_currency(next_month_total),
        }

    @classmethod
    def get_recurring_expense_analysis(cls, household: Household) -> Dict[str, Any]:
        """
        Analyzes predictable recurring commitments configured in apps.recurring.
        Converts frequencies to an estimated monthly commitment.
        """
        active_schedules = RecurringSchedule.objects.filter(
            household=household,
            is_active=True,
        ).select_related("assigned_to")

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
                "frequency": sched.frequency,
                "frequency_display": sched.get_frequency_display(),
                "expected_amount": cls._format_currency(amt),
                "monthly_equivalent": cls._format_currency(monthly_equiv),
                "next_due_date": sched.next_due_date.isoformat() if sched.next_due_date else None,
            })

        return {
            "active_schedules_count": active_schedules.count(),
            "estimated_monthly_recurring_total": cls._format_currency(monthly_commitment),
            "schedules": schedules_list,
        }

    @classmethod
    def get_spending_forecast(cls, household: Household) -> Dict[str, Any]:
        """
        Produces a forward-looking 30-day expense forecast.
        DISCLOSURE MANDATE: Assumptions are clearly stated and derived strictly
        from active recurring commitments + 3-month moving average of non-recurring utility bills.
        """
        recurring_data = cls.get_recurring_expense_analysis(household)
        recurring_committed = Decimal(recurring_data["estimated_monthly_recurring_total"])

        # 3-month moving average of variable bills (bills not associated with recurring schedules)
        today = timezone.localdate()
        three_months_ago = today - timedelta(days=90)
        variable_bills = Bill.objects.filter(
            household=household,
            due_date__gte=three_months_ago,
            recurring_occurrence__isnull=True,
        )
        total_variable = variable_bills.aggregate(sum=Sum("amount"))["sum"] or Decimal("0.00")
        variable_monthly_avg = (total_variable / Decimal("3.0")).quantize(Decimal("0.01"))

        forecast_total = (recurring_committed + variable_monthly_avg).quantize(Decimal("0.01"))

        assumptions_text = (
            f"Forecast Methodology & Disclosed Assumptions: "
            f"Projected {forecast_total:.2f} INR is calculated from: "
            f"1) Committed active recurring schedules ({recurring_committed:.2f} INR), and "
            f"2) 3-month historical moving average of non-recurring variable expenses ({variable_monthly_avg:.2f} INR)."
        )

        return {
            "projected_monthly_spend": cls._format_currency(forecast_total),
            "committed_recurring_component": cls._format_currency(recurring_committed),
            "variable_moving_avg_component": cls._format_currency(variable_monthly_avg),
            "disclosed_assumptions": assumptions_text,
        }

    @classmethod
    def get_full_spending_summary(cls, household: Household) -> Dict[str, Any]:
        """
        Compiles the complete, deterministic financial metrics dataset for the household.
        All figures are computed in Django/Postgres and passed to AI for explanation.
        """
        monthly = cls.get_monthly_comparison(household)
        categories = cls.get_category_breakdown(household, months=6)
        hikes = cls.get_increasing_bills_and_rate_hikes(household)
        obligations = cls.get_upcoming_obligations(household)
        recurring = cls.get_recurring_expense_analysis(household)
        forecast = cls.get_spending_forecast(household)

        electricity_history = cls.get_category_history(household, BillCategory.ELECTRICITY, months=6)

        return {
            "household_name": household.name,
            "monthly_comparison": monthly,
            "category_summary": categories,
            "electricity_six_months": electricity_history,
            "increasing_bills": hikes,
            "upcoming_obligations": obligations,
            "recurring_expenses": recurring,
            "forecast": forecast,
        }

    @classmethod
    def generate_ai_explanation(cls, summary_data: Dict[str, Any], household: Household, user=None) -> str:
        """
        Generates a natural-language executive narrative explaining the
        deterministically calculated spending summary.
        Uses hosted Gemini API when configured, or deterministic structured generator when offline/testing.
        """
        api_key = getattr(settings, "GEMINI_API_KEY", "") or ""
        backend = getattr(settings, "BILL_EXTRACTION_BACKEND", "mock")

        if api_key and backend != "mock":
            try:
                from google import genai
                from google.genai import types

                client = genai.Client(api_key=settings.GEMINI_API_KEY)
                prompt = (
                    "You are a professional financial advisor reviewing a household's utility and bill expenses.\n"
                    "RULES:\n"
                    "1. Use ONLY the exact numbers provided in the JSON data below. NEVER change or invent figures.\n"
                    "2. Explain monthly spending trends, highlight the top expense category, note any provider rate increases, "
                    "and summarize upcoming obligations and forecast assumptions.\n"
                    "3. Format your response cleanly in Github-flavored markdown with clear headers and bullet points.\n\n"
                    f"DATA:\n{summary_data}"
                )

                response = client.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=[prompt],
                    config=types.GenerateContentConfig(temperature=0.2),
                )

                # Record token usage
                prompt_toks = getattr(response.usage_metadata, "prompt_token_count", 300) or 300
                compl_toks = getattr(response.usage_metadata, "candidates_token_count", 200) or 200
                AIUsageRecord.objects.create(
                    household=household,
                    user=user,
                    provider="google-gemini",
                    model_name="gemini-2.5-flash",
                    prompt_tokens=prompt_toks,
                    completion_tokens=compl_toks,
                    total_tokens=prompt_toks + compl_toks,
                    estimated_cost_usd=Decimal(str((prompt_toks + compl_toks) * 0.0000003)),
                    operation_type="SPENDING_INSIGHTS_EXPLANATION",
                )
                return response.text
            except Exception as exc:
                logger.warning(f"Gemini API call failed for insights explanation: {exc}. Using deterministic generator.")

        # Deterministic offline narrative generator
        m = summary_data["monthly_comparison"]
        c = summary_data["category_summary"]
        h = summary_data["increasing_bills"]
        o = summary_data["upcoming_obligations"]
        r = summary_data["recurring_expenses"]
        f = summary_data["forecast"]

        top_cat_text = "No category data"
        if c.get("top_category"):
            tc = c["top_category"]
            top_cat_text = f"**{tc['category_display']}** ({tc['total_amount']} INR, representing {tc['percentage_of_total']} of total spend)"

        hikes_text = "No significant provider rate hikes detected."
        if h:
            hikes_items = [f"- **{x['provider_name']}** ({x['category_display']}): increased from {x['previous_amount']} to {x['latest_amount']} INR ({x['percentage_increase']})" for x in h]
            hikes_text = "\n".join(hikes_items)

        explanation = (
            f"### Household Financial Insights for {summary_data['household_name']}\n\n"
            f"#### 1. Monthly Spending Trend\n"
            f"- **Current Month ({m['current_month']}):** Total billed is **{m['current_total_billed']} INR** "
            f"({m['current_total_paid']} INR paid, **{m['current_outstanding']} INR** outstanding across {m['current_bills_count']} bills).\n"
            f"- **Comparison with Previous Month ({m['previous_month']}):** Spending has **{m['direction'].lower()}** "
            f"by **{m['difference']} INR** ({m['percentage_change']}) from {m['previous_total_billed']} INR.\n\n"
            f"#### 2. Category Distribution & Top Costs\n"
            f"- **Highest Expense Category:** {top_cat_text}.\n"
            f"- **Historical 6-Month Grand Total:** {c['grand_total']} INR across {len(c['categories'])} active utility categories.\n\n"
            f"#### 3. Provider Rate Increases\n"
            f"{hikes_text}\n\n"
            f"#### 4. Upcoming Obligations & Recurring Commitments\n"
            f"- **Total Outstanding Debt:** **{o['total_outstanding_amount']} INR** "
            f"(including **{o['overdue_total']} INR** across {o['overdue_count']} overdue bills).\n"
            f"- **Due Next Week (7 Days):** **{o['due_next_week_total']} INR** ({o['due_next_week_count']} bills).\n"
            f"- **Active Recurring Subscriptions:** {r['active_schedules_count']} schedules totaling **{r['estimated_monthly_recurring_total']} INR/month**.\n\n"
            f"#### 5. 30-Day Forward Forecast\n"
            f"- **Projected Next Month Expenses:** **{f['projected_monthly_spend']} INR**\n"
            f"- _{f['disclosed_assumptions']}_\n"
        )
        return explanation

    @classmethod
    def detect_and_save_insights(cls, household: Household) -> List[SpendingInsight]:
        """
        Executes automated analytical checks and persists proactive SpendingInsight
        records for expense spikes, provider rate increases, and upcoming forecasts.
        """
        created = []
        today = timezone.localdate()

        # 1. Category Spikes (>= 35% above historical moving average)
        six_months_ago = today - timedelta(days=180)
        thirty_days_ago = today - timedelta(days=30)

        hist_bills = Bill.objects.filter(
            household=household,
            due_date__gte=six_months_ago,
            due_date__lt=thirty_days_ago,
        )
        recent_bills = Bill.objects.filter(
            household=household,
            due_date__gte=thirty_days_ago,
        )

        cat_avgs = (
            hist_bills.values("category")
            .annotate(avg=Avg("amount"), count=Count("id"))
        )
        avg_map = {row["category"]: row["avg"] for row in cat_avgs if row["count"] >= 2}

        for bill in recent_bills:
            if bill.category in avg_map:
                hist_avg = avg_map[bill.category]
                diff = bill.amount - hist_avg
                if bill.amount > (hist_avg * Decimal("1.35")) and diff >= Decimal("200.00"):
                    exists = SpendingInsight.objects.filter(
                        household=household,
                        insight_type=SpendingInsight.InsightType.EXPENSE_SPIKE,
                        category=bill.category,
                        is_dismissed=False,
                    ).exists()
                    if not exists:
                        severity = (
                            SpendingInsight.Severity.CRITICAL
                            if bill.amount > (hist_avg * Decimal("1.70"))
                            else SpendingInsight.Severity.WARNING
                        )
                        insight = SpendingInsight.objects.create(
                            household=household,
                            insight_type=SpendingInsight.InsightType.EXPENSE_SPIKE,
                            title=f"Unusual {bill.get_category_display()} Bill from {bill.provider_name}",
                            description=(
                                f"The recent {bill.get_category_display()} bill of {bill.amount} {bill.currency} "
                                f"is {diff:.2f} {bill.currency} (+{(diff / hist_avg * 100):.0f}%) higher than your "
                                f"historical average of {hist_avg:.2f} {bill.currency}."
                            ),
                            severity=severity,
                            category=bill.category,
                            amount_difference=diff,
                        )
                        created.append(insight)

        # 2. Provider Rate Increases
        hikes = cls.get_increasing_bills_and_rate_hikes(household)
        for h in hikes:
            title = f"Rate Increase Detected: {h['provider_name']}"
            exists = SpendingInsight.objects.filter(
                household=household,
                insight_type=SpendingInsight.InsightType.RATE_INCREASE,
                title=title,
                is_dismissed=False,
            ).exists()
            if not exists:
                insight = SpendingInsight.objects.create(
                    household=household,
                    insight_type=SpendingInsight.InsightType.RATE_INCREASE,
                    title=title,
                    description=(
                        f"{h['provider_name']} increased their {h['category_display']} charge from "
                        f"{h['previous_amount']} INR to {h['latest_amount']} INR ({h['percentage_increase']}, "
                        f"+{h['increase_amount']} INR)."
                    ),
                    severity=SpendingInsight.Severity.WARNING,
                    category=h["category"],
                    amount_difference=Decimal(h["increase_amount"]),
                )
                created.append(insight)

        # 3. Upcoming 30-Day Expense Forecast
        forecast = cls.get_spending_forecast(household)
        forecast_title = f"Upcoming 30-Day Expense Projection ({today.strftime('%B %Y')})"
        exists_forecast = SpendingInsight.objects.filter(
            household=household,
            insight_type=SpendingInsight.InsightType.UPCOMING_EXPENSE_FORECAST,
            title=forecast_title,
            is_dismissed=False,
        ).exists()

        if not exists_forecast and Decimal(forecast["projected_monthly_spend"]) > Decimal("0.00"):
            insight = SpendingInsight.objects.create(
                household=household,
                insight_type=SpendingInsight.InsightType.UPCOMING_EXPENSE_FORECAST,
                title=forecast_title,
                description=(
                    f"Projected next month expenses total {forecast['projected_monthly_spend']} INR. "
                    f"{forecast['disclosed_assumptions']}"
                ),
                severity=SpendingInsight.Severity.INFO,
                amount_difference=Decimal(forecast["projected_monthly_spend"]),
            )
            created.append(insight)

        return created
