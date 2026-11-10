import datetime
from decimal import Decimal
from datetime import timedelta
from unittest.mock import patch
import pytest
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.households.models import Household, HouseholdMembership
from apps.bills.models import Bill, BillCategory, BillStatus
from apps.recurring.models import RecurringSchedule, RecurrenceFrequency
from apps.ai.models import SpendingInsight
from apps.ai.services.spending_insights_service import SpendingInsightsService


@pytest.fixture(autouse=True)
def mock_localdate():
    with patch("django.utils.timezone.localdate", return_value=datetime.date(2026, 11, 10)):
        yield datetime.date(2026, 11, 10)


@pytest.fixture
def owner_user(db):
    return User.objects.create_user(
        username="owner_phase6",
        email="owner_phase6@example.com",
        password="Password123!",
        first_name="Alice",
        last_name="Owner",
    )


@pytest.fixture
def member_user(db):
    return User.objects.create_user(
        username="member_phase6",
        email="member_phase6@example.com",
        password="Password123!",
        first_name="Bob",
        last_name="Member",
    )


@pytest.fixture
def outsider_user(db):
    return User.objects.create_user(
        username="outsider_phase6",
        email="outsider_phase6@example.com",
        password="Password123!",
        first_name="Eve",
        last_name="Outsider",
    )


@pytest.fixture
def test_household(owner_user, member_user):
    hh = Household.objects.create(name="Phase 6 Manor", created_by=owner_user)
    HouseholdMembership.objects.create(household=hh, user=owner_user, role=HouseholdMembership.Role.OWNER)
    HouseholdMembership.objects.create(household=hh, user=member_user, role=HouseholdMembership.Role.MEMBER)
    return hh


@pytest.fixture
def historical_bills(test_household, owner_user, member_user):
    # Fixed deterministic dates:
    # Today is 2026-11-10
    # Previous month: 2026-10
    prev_date = datetime.date(2026, 10, 15)
    b_prev1 = Bill.objects.create(
        household=test_household,
        uploaded_by=owner_user,
        assigned_to=member_user,
        provider_name="Tata Power",
        category=BillCategory.ELECTRICITY,
        amount=Decimal("2000.00"),
        total_paid=Decimal("2000.00"),
        outstanding_balance=Decimal("0.00"),
        status=BillStatus.PAID,
        due_date=prev_date,
    )
    b_prev2 = Bill.objects.create(
        household=test_household,
        uploaded_by=owner_user,
        assigned_to=member_user,
        provider_name="Airtel Broadband",
        category=BillCategory.INTERNET,
        amount=Decimal("1000.00"),
        total_paid=Decimal("1000.00"),
        outstanding_balance=Decimal("0.00"),
        status=BillStatus.PAID,
        due_date=prev_date,
    )

    # Current month: 2026-11
    # b_curr1: Tata Power, due Nov 25 (due in 15 days, so NOT in due_next_week, but in due_next_month)
    b_curr1 = Bill.objects.create(
        household=test_household,
        uploaded_by=owner_user,
        assigned_to=member_user,
        provider_name="Tata Power",
        category=BillCategory.ELECTRICITY,
        amount=Decimal("2600.00"),  # Increased from 2000 to 2600 (+30%)
        total_paid=Decimal("1000.00"),
        outstanding_balance=Decimal("1600.00"),
        status=BillStatus.PARTIALLY_PAID,
        due_date=datetime.date(2026, 11, 25),
    )
    # b_curr2: Airtel Broadband, due Nov 14 (due in 4 days, so IS in due_next_week and due_next_month)
    b_curr2 = Bill.objects.create(
        household=test_household,
        uploaded_by=owner_user,
        assigned_to=member_user,
        provider_name="Airtel Broadband",
        category=BillCategory.INTERNET,
        amount=Decimal("1100.00"),  # Increased from 1000 to 1100 (+10%)
        total_paid=Decimal("0.00"),
        outstanding_balance=Decimal("1100.00"),
        status=BillStatus.PENDING,
        due_date=datetime.date(2026, 11, 14),
    )

    # Overdue bill from September (due 46 days ago, overdue, outside current & previous month)
    b_overdue = Bill.objects.create(
        household=test_household,
        uploaded_by=owner_user,
        assigned_to=member_user,
        provider_name="City Municipal Water",
        category=BillCategory.WATER,
        amount=Decimal("500.00"),
        total_paid=Decimal("0.00"),
        outstanding_balance=Decimal("500.00"),
        status=BillStatus.OVERDUE,
        due_date=datetime.date(2026, 9, 25),
    )

    return [b_prev1, b_prev2, b_curr1, b_curr2, b_overdue]


@pytest.mark.django_db
class TestPhase6DeterministicCalculations:
    def test_monthly_comparison_calculations(self, test_household, historical_bills):
        """
        Verifies month-over-month total spending calculations.
        Previous: 3,000 (2000 + 1000). Current: 3,700 (2600 + 1100).
        Difference: +700 (+23.3%), direction: INCREASED.
        """
        res = SpendingInsightsService.get_monthly_comparison(test_household)

        assert Decimal(res["previous_total_billed"]) == Decimal("3000.00")
        assert Decimal(res["current_total_billed"]) == Decimal("3700.00")
        assert Decimal(res["current_total_paid"]) == Decimal("1000.00")
        assert Decimal(res["current_outstanding"]) == Decimal("2700.00")
        assert Decimal(res["difference"]) == Decimal("700.00")
        assert res["direction"] == "INCREASED"
        assert "+23.3%" in res["percentage_change"]

    def test_category_breakdown_and_top_category(self, test_household, historical_bills):
        """
        Aggregates category spending over past 6 months.
        Tata Power (4600) + Airtel (2100) + Water (500) = 7,200.
        Top category must be ELECTRICITY.
        """
        res = SpendingInsightsService.get_category_breakdown(test_household, months=6)

        assert Decimal(res["grand_total"]) == Decimal("7200.00")
        assert res["top_category"]["category"] == BillCategory.ELECTRICITY
        assert Decimal(res["top_category"]["total_amount"]) == Decimal("4600.00")
        assert res["top_category"]["bills_count"] == 2

    def test_six_month_category_history_electricity(self, test_household, historical_bills):
        """
        Calculates 6-month historical spend specifically for Electricity.
        2 bills: 2,000 and 2,600 = 4,600 total, average 2,300.
        """
        res = SpendingInsightsService.get_category_history(test_household, BillCategory.ELECTRICITY, months=6)

        assert res["category"] == BillCategory.ELECTRICITY
        assert Decimal(res["total_spent"]) == Decimal("4600.00")
        assert Decimal(res["average_spent"]) == Decimal("2300.00")
        assert res["bills_count"] == 2

    def test_increasing_bills_and_rate_hikes_detection(self, test_household, historical_bills):
        """
        Detects rate increases by provider:
        - Tata Power: 2,000 -> 2,600 (+600.00, +30.0%)
        - Airtel Broadband: 1,000 -> 1,100 (+100.00, +10.0%)
        """
        hikes = SpendingInsightsService.get_increasing_bills_and_rate_hikes(test_household)
        assert len(hikes) == 2

        providers = {h["provider_name"]: h for h in hikes}
        assert "Tata Power" in providers
        assert Decimal(providers["Tata Power"]["increase_amount"]) == Decimal("600.00")
        assert "+30.0%" in providers["Tata Power"]["percentage_increase"]

        assert "Airtel Broadband" in providers
        assert Decimal(providers["Airtel Broadband"]["increase_amount"]) == Decimal("100.00")
        assert "+10.0%" in providers["Airtel Broadband"]["percentage_increase"]

    def test_upcoming_obligations_and_overdue_obligations(self, test_household, historical_bills):
        """
        Verifies upcoming obligations calculation:
        - Airtel bill due in 4 days: 1,100 outstanding (due next week)
        - Overdue water bill: 500 outstanding
        - Total outstanding: 1,600 (Tata Power) + 1,100 (Airtel) + 500 (Water) = 3,200.
        """
        res = SpendingInsightsService.get_upcoming_obligations(test_household)

        assert Decimal(res["total_outstanding_amount"]) == Decimal("3200.00")
        assert res["overdue_count"] == 1
        assert Decimal(res["overdue_total"]) == Decimal("500.00")

        # Due next week (Airtel in 4 days)
        assert res["due_next_week_count"] == 1
        assert Decimal(res["due_next_week_total"]) == Decimal("1100.00")
        assert res["due_next_week_bills"][0]["provider_name"] == "Airtel Broadband"

    def test_recurring_expense_analysis(self, test_household, owner_user, member_user):
        """
        Active recurring schedules are converted to monthly equivalent totals.
        """
        RecurringSchedule.objects.create(
            household=test_household,
            created_by=owner_user,
            assigned_to=member_user,
            provider_name="Netflix Family",
            category=BillCategory.OTHER,
            frequency=RecurrenceFrequency.MONTHLY,
            expected_amount=Decimal("649.00"),
            next_due_date=datetime.date(2026, 12, 1),
            is_active=True,
        )
        RecurringSchedule.objects.create(
            household=test_household,
            created_by=owner_user,
            assigned_to=member_user,
            provider_name="Gym Membership",
            category=BillCategory.OTHER,
            frequency=RecurrenceFrequency.QUARTERLY,
            expected_amount=Decimal("3000.00"),  # 1000/month
            next_due_date=datetime.date(2026, 12, 1),
            is_active=True,
        )

        res = SpendingInsightsService.get_recurring_expense_analysis(test_household)
        assert res["active_schedules_count"] == 2
        # 649 + 1000 = 1649.00
        assert Decimal(res["estimated_monthly_recurring_total"]) == Decimal("1649.00")

    def test_spending_forecast_with_disclosed_assumptions(self, test_household, owner_user, member_user, historical_bills):
        """
        Forecast must clearly state its methodology and disclosed assumptions.
        """
        RecurringSchedule.objects.create(
            household=test_household,
            created_by=owner_user,
            assigned_to=member_user,
            provider_name="Spotify Family",
            category=BillCategory.OTHER,
            frequency=RecurrenceFrequency.MONTHLY,
            expected_amount=Decimal("179.00"),
            next_due_date=datetime.date(2026, 12, 1),
            is_active=True,
        )

        res = SpendingInsightsService.get_spending_forecast(test_household)

        assert Decimal(res["committed_recurring_component"]) == Decimal("179.00")
        assert Decimal(res["projected_monthly_spend"]) > Decimal("179.00")
        assert "Forecast Methodology & Disclosed Assumptions" in res["disclosed_assumptions"]
        assert "179.00" in res["disclosed_assumptions"]


@pytest.mark.django_db
class TestPhase6AIExplanationAndGuardrails:
    def test_ai_explanation_uses_exact_django_figures_without_hallucination(self, test_household, historical_bills):
        """
        The generated explanation narrative must contain the exact calculated figures.
        """
        summary_data = SpendingInsightsService.get_full_spending_summary(test_household)
        explanation = SpendingInsightsService.generate_ai_explanation(summary_data, test_household)

        # Asserts exact figures appear in narrative
        assert "3700.00 INR" in explanation
        assert "3000.00 INR" in explanation
        assert "+23.3%" in explanation
        assert "Tata Power" in explanation
        assert "+30.0%" in explanation
        assert "3200.00 INR" in explanation  # Total debt
        assert "500.00 INR" in explanation   # Overdue debt

    def test_detect_and_save_insights_persists_actionable_alerts(self, test_household, historical_bills):
        """
        detect_and_save_insights creates SpendingInsight rows for rate hikes and projections.
        """
        insights = SpendingInsightsService.detect_and_save_insights(test_household)
        assert len(insights) >= 2

        types = [i.insight_type for i in insights]
        assert SpendingInsight.InsightType.RATE_INCREASE in types
        assert SpendingInsight.InsightType.UPCOMING_EXPENSE_FORECAST in types

        # Rows saved to database
        db_insights = SpendingInsight.objects.filter(household=test_household)
        assert db_insights.count() >= 2


@pytest.mark.django_db
class TestPhase6APIAndWebInterface:
    def test_api_spending_insights_summary_endpoint(self, member_user, outsider_user, test_household, historical_bills):
        """
        GET /api/v1/ai/insights/summary/ returns calculated figures and narrative.
        Foreign users cannot inspect household spending summary.
        """
        client = APIClient()

        # 1. Member access succeeds
        client.force_authenticate(user=member_user)
        res = client.get(f"/api/v1/ai/insights/summary/?household={test_household.id}")
        assert res.status_code == status.HTTP_200_OK
        assert "summary" in res.data
        assert "ai_explanation" in res.data
        assert res.data["summary"]["monthly_comparison"]["current_total_billed"] == "3700.00"

        # 2. Outsider access denied
        client.force_authenticate(user=outsider_user)
        res_outsider = client.get(f"/api/v1/ai/insights/summary/?household={test_household.id}")
        assert res_outsider.status_code == status.HTTP_404_NOT_FOUND

    def test_web_spending_insights_view_and_dismiss_flow(self, member_user, test_household, historical_bills):
        """
        Web views for spending insights render data and allow dismiss action.
        """
        client = APIClient()
        client.force_login(member_user)

        # 1. Access web insights page
        res = client.get(f"/insights/?household={test_household.id}")
        assert res.status_code == status.HTTP_200_OK
        content = res.content.decode()
        assert "AI Spending Intelligence" in content
        assert "3700.00" in content
        assert "Tata Power" in content

        # 2. Trigger on-demand generation
        res_gen = client.post("/insights/generate/", {"household_id": test_household.id})
        assert res_gen.status_code == status.HTTP_302_FOUND

        active_insight = SpendingInsight.objects.filter(household=test_household, is_dismissed=False).first()
        assert active_insight is not None

        # 3. Dismiss insight
        res_dismiss = client.post(f"/insights/{active_insight.id}/dismiss/")
        assert res_dismiss.status_code == status.HTTP_302_FOUND
        active_insight.refresh_from_db()
        assert active_insight.is_dismissed is True
