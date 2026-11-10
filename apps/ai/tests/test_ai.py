import pytest
from decimal import Decimal
from datetime import timedelta
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.households.models import Household, HouseholdMembership
from apps.bills.models import Bill, BillCategory, BillStatus
from apps.payments.models import Payment, PaymentMethod
from apps.ai.models import (
    AIConversation,
    AIMessage,
    AIToolExecution,
    AIUsageRecord,
    SpendingInsight,
)
from apps.ai.services.tools import HouseholdAITools, execute_tool_call
from apps.ai.services.assistant import AIAssistantService
from apps.ai.services.anomaly_detector import SpendingAnomalyDetector


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def member(db):
    return User.objects.create_user(
        username="ai_member",
        email="ai_member@example.com",
        password="Password123!",
        first_name="AI",
        last_name="Member",
    )


@pytest.fixture
def outsider(db):
    return User.objects.create_user(
        username="ai_outsider",
        email="ai_outsider@example.com",
        password="Password123!",
        first_name="AI",
        last_name="Outsider",
    )


@pytest.fixture
def household(member):
    hh = Household.objects.create(name="Greenwood Villa", created_by=member)
    HouseholdMembership.objects.create(household=hh, user=member, role=HouseholdMembership.Role.OWNER)
    return hh


@pytest.fixture
def foreign_household(outsider):
    hh = Household.objects.create(name="Foreign Villa", created_by=outsider)
    HouseholdMembership.objects.create(household=hh, user=outsider, role=HouseholdMembership.Role.OWNER)
    return hh


@pytest.fixture
def sample_bills(household, member):
    today = timezone.localdate()
    b1 = Bill.objects.create(
        household=household,
        uploaded_by=member,
        assigned_to=member,
        provider_name="Adani Electricity",
        category=BillCategory.ELECTRICITY,
        amount=Decimal("3000.00"),
        total_paid=Decimal("1000.00"),
        outstanding_balance=Decimal("2000.00"),
        status=BillStatus.PARTIALLY_PAID,
        due_date=today + timedelta(days=5),
    )
    b2 = Bill.objects.create(
        household=household,
        uploaded_by=member,
        assigned_to=member,
        provider_name="ACT Fibernet",
        category=BillCategory.INTERNET,
        amount=Decimal("1200.00"),
        total_paid=Decimal("0.00"),
        outstanding_balance=Decimal("1200.00"),
        status=BillStatus.PENDING,
        due_date=today + timedelta(days=12),
    )
    return [b1, b2]


@pytest.mark.django_db
class TestAITools:
    def test_get_unpaid_bills(self, household, member, sample_bills):
        res = HouseholdAITools.get_unpaid_bills(household, member)
        assert res["unpaid_bills_count"] == 2
        assert Decimal(res["total_outstanding_amount"]) == Decimal("3200.00")
        assert len(res["bills"]) == 2

    def test_get_upcoming_bills(self, household, member, sample_bills):
        res = HouseholdAITools.get_upcoming_bills(household, member, days=10)
        assert res["upcoming_count"] == 1
        assert res["bills"][0]["provider_name"] == "Adani Electricity"

    def test_get_monthly_spending(self, household, member, sample_bills):
        today = timezone.localdate()
        res = HouseholdAITools.get_monthly_spending(household, member, year=today.year, month=today.month)
        assert Decimal(res["total_billed"]) == Decimal("4200.00")
        assert Decimal(res["total_paid"]) == Decimal("1000.00")
        assert Decimal(res["total_outstanding"]) == Decimal("3200.00")
        assert len(res["category_breakdown"]) == 2

    def test_compare_category_spending(self, household, member, sample_bills):
        res = HouseholdAITools.compare_category_spending(household, member, months=3)
        assert Decimal(res["grand_total"]) == Decimal("4200.00")
        assert len(res["categories"]) == 2

    def test_explain_bill_with_payments(self, household, member, sample_bills):
        bill = sample_bills[0]
        Payment.objects.create(
            bill=bill,
            household=household,
            paid_by=member,
            amount=Decimal("1000.00"),
            payment_method=PaymentMethod.UPI,
            transaction_reference="UPI/123456",
        )
        res = HouseholdAITools.explain_bill(household, member, bill.id)
        assert res["bill_id"] == bill.id
        assert res["provider_name"] == "Adani Electricity"
        assert res["payments_count"] == 1
        assert res["payments"][0]["transaction_reference"] == "UPI/123456"

    def test_tenant_isolation_foreign_user_cannot_run_tool(self, household, outsider):
        res = HouseholdAITools.get_unpaid_bills(household, outsider)
        assert "error" in res
        assert "permission" in res["error"].lower()


@pytest.mark.django_db
class TestAIAssistantService:
    def test_conversational_turn_executes_tools_and_tracks_usage(self, household, member, sample_bills):
        conv = AIConversation.objects.create(household=household, user=member, title="Billing Q&A")

        prompt = "What are my pending unpaid bills?"
        assistant_msg, tool_execs = AIAssistantService.send_message(conv, member, prompt)

        assert assistant_msg.role == AIMessage.Role.ASSISTANT
        assert "3200" in assistant_msg.content or "2" in assistant_msg.content
        assert len(tool_execs) == 1
        assert tool_execs[0].tool_name == "get_unpaid_bills"
        assert tool_execs[0].is_success is True

        # Check AIUsageRecord exists
        usage = AIUsageRecord.objects.filter(household=household).first()
        assert usage is not None
        assert usage.operation_type == "CHAT_ASSISTANT"
        assert usage.total_tokens > 0

    def test_explain_bill_intent_in_conversation(self, household, member, sample_bills):
        conv = AIConversation.objects.create(household=household, user=member)
        target_bill = sample_bills[0]

        prompt = f"Please explain bill #{target_bill.id}"
        assistant_msg, tool_execs = AIAssistantService.send_message(conv, member, prompt)

        assert len(tool_execs) == 1
        assert tool_execs[0].tool_name == "explain_bill"
        assert "Adani Electricity" in assistant_msg.content


@pytest.mark.django_db
class TestSpendingAnomalyDetector:
    def test_expense_spike_detection(self, household, member):
        today = timezone.localdate()
        # Historical baseline: 3 bills of ~1000 INR
        for days_back in [150, 100, 60]:
            Bill.objects.create(
                household=household,
                uploaded_by=member,
                provider_name="City Power",
                category=BillCategory.ELECTRICITY,
                amount=Decimal("1000.00"),
                due_date=today - timedelta(days=days_back),
            )

        # Recent spike: 2200 INR (120% spike > 35% threshold and > 200 diff)
        Bill.objects.create(
            household=household,
            uploaded_by=member,
            provider_name="City Power",
            category=BillCategory.ELECTRICITY,
            amount=Decimal("2200.00"),
            due_date=today - timedelta(days=5),
        )

        insights = SpendingAnomalyDetector.analyze_household(household)
        assert len(insights) >= 1
        spike_insight = next(i for i in insights if i.insight_type == SpendingInsight.InsightType.EXPENSE_SPIKE)
        assert spike_insight.category == BillCategory.ELECTRICITY
        assert spike_insight.severity in (SpendingInsight.Severity.WARNING, SpendingInsight.Severity.CRITICAL)
        assert "City Power" in spike_insight.title

        # Repeated run should NOT create duplicate insight
        repeated_insights = SpendingAnomalyDetector.analyze_household(household)
        spike_count = SpendingInsight.objects.filter(
            household=household,
            insight_type=SpendingInsight.InsightType.EXPENSE_SPIKE,
        ).count()
        assert spike_count == 1


@pytest.mark.django_db
class TestAIChatAPI:
    def test_create_and_list_conversations(self, api_client, household, member, outsider):
        api_client.force_authenticate(user=member)
        res_create = api_client.post(
            "/api/v1/ai/conversations/",
            {"household": household.id, "title": "Family Budgeting"},
        )
        assert res_create.status_code == status.HTTP_201_CREATED
        conv_id = res_create.data["id"]

        # List conversations
        res_list = api_client.get("/api/v1/ai/conversations/")
        assert res_list.status_code == status.HTTP_200_OK
        assert len(res_list.data["results"]) == 1

        # Outsider cannot see conversation
        api_client.force_authenticate(user=outsider)
        res_outsider = api_client.get(f"/api/v1/ai/conversations/{conv_id}/")
        assert res_outsider.status_code == status.HTTP_404_NOT_FOUND

    def test_send_message_endpoint_triggers_tools(self, api_client, household, member, sample_bills):
        conv = AIConversation.objects.create(household=household, user=member)
        api_client.force_authenticate(user=member)

        url = f"/api/v1/ai/conversations/{conv.id}/send_message/"
        res = api_client.post(url, {"prompt": "Show my monthly spending"})

        assert res.status_code == status.HTTP_200_OK
        assert "message" in res.data
        assert res.data["tool_executions_count"] >= 1
        assert res.data["message"]["role"] == "ASSISTANT"

    def test_dismiss_insight_endpoint(self, api_client, household, member):
        insight = SpendingInsight.objects.create(
            household=household,
            title="Spike Alert",
            description="Test alert",
        )
        api_client.force_authenticate(user=member)
        url_dismiss = f"/api/v1/ai/insights/{insight.id}/dismiss/"
        res = api_client.post(url_dismiss)
        assert res.status_code == status.HTTP_200_OK
        assert res.data["is_dismissed"] is True

        insight.refresh_from_db()
        assert insight.is_dismissed is True
