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
from apps.payments.models import Payment, PaymentMethod
from apps.recurring.models import RecurringSchedule, RecurrenceFrequency
from apps.ai.models import AIConversation, AIMessage, AIToolExecution, AIUsageRecord
from apps.ai.services.tools import HouseholdAITools, execute_tool_call
from apps.ai.services.assistant import AIAssistantService


@pytest.fixture(autouse=True)
def mock_localdate():
    with patch("django.utils.timezone.localdate", return_value=datetime.date(2026, 11, 10)):
        yield datetime.date(2026, 11, 10)


@pytest.fixture
def owner_user(db):
    return User.objects.create_user(
        username="owner_phase7",
        email="owner_phase7@example.com",
        password="Password123!",
        first_name="Alice",
        last_name="Owner",
    )


@pytest.fixture
def member_user(db):
    return User.objects.create_user(
        username="member_phase7",
        email="member_phase7@example.com",
        password="Password123!",
        first_name="Bob",
        last_name="Member",
    )


@pytest.fixture
def outsider_user(db):
    return User.objects.create_user(
        username="outsider_phase7",
        email="outsider_phase7@example.com",
        password="Password123!",
        first_name="Eve",
        last_name="Outsider",
    )


@pytest.fixture
def test_household(owner_user, member_user):
    hh = Household.objects.create(name="Phase 7 Residency", created_by=owner_user)
    HouseholdMembership.objects.create(household=hh, user=owner_user, role=HouseholdMembership.Role.OWNER)
    HouseholdMembership.objects.create(household=hh, user=member_user, role=HouseholdMembership.Role.MEMBER)
    return hh


@pytest.fixture
def foreign_household(outsider_user):
    hh = Household.objects.create(name="Foreign Manor", created_by=outsider_user)
    HouseholdMembership.objects.create(household=hh, user=outsider_user, role=HouseholdMembership.Role.OWNER)
    return hh


@pytest.fixture
def sample_financial_data(test_household, owner_user, member_user):
    # Today is fixed to 2026-11-10
    # 1. Past paid bill (Oct 2026)
    b_oct = Bill.objects.create(
        household=test_household,
        uploaded_by=owner_user,
        assigned_to=member_user,
        provider_name="Tata Power",
        category=BillCategory.ELECTRICITY,
        amount=Decimal("2000.00"),
        total_paid=Decimal("2000.00"),
        outstanding_balance=Decimal("0.00"),
        status=BillStatus.PAID,
        due_date=datetime.date(2026, 10, 15),
    )

    # 2. Upcoming bill for Member (Nov 15, due in 5 days, so within 7 days)
    b_up_member = Bill.objects.create(
        household=test_household,
        uploaded_by=owner_user,
        assigned_to=member_user,
        provider_name="Tata Power",
        category=BillCategory.ELECTRICITY,
        amount=Decimal("2600.00"),
        total_paid=Decimal("600.00"),
        outstanding_balance=Decimal("2000.00"),
        status=BillStatus.PARTIALLY_PAID,
        due_date=datetime.date(2026, 11, 15),
    )

    # 3. Upcoming bill for Owner (Nov 20, due in 10 days)
    b_up_owner = Bill.objects.create(
        household=test_household,
        uploaded_by=owner_user,
        assigned_to=owner_user,
        provider_name="Airtel Broadband",
        category=BillCategory.INTERNET,
        amount=Decimal("1200.00"),
        total_paid=Decimal("0.00"),
        outstanding_balance=Decimal("1200.00"),
        status=BillStatus.PENDING,
        due_date=datetime.date(2026, 11, 20),
    )

    # 4. Overdue bill for Member (Water, due Sept 25, 46 days overdue)
    b_overdue = Bill.objects.create(
        household=test_household,
        uploaded_by=owner_user,
        assigned_to=member_user,
        provider_name="City Water Dept",
        category=BillCategory.WATER,
        amount=Decimal("750.00"),
        total_paid=Decimal("0.00"),
        outstanding_balance=Decimal("750.00"),
        status=BillStatus.OVERDUE,
        due_date=datetime.date(2026, 9, 25),
    )

    # 5. Payment on b_up_member
    p1 = Payment.objects.create(
        household=test_household,
        bill=b_up_member,
        paid_by=member_user,
        amount=Decimal("600.00"),
        payment_date=datetime.date(2026, 11, 8),
        payment_method=PaymentMethod.UPI,
        transaction_reference="UPI/2026/888999",
    )

    # 6. Recurring schedule
    sched = RecurringSchedule.objects.create(
        household=test_household,
        created_by=owner_user,
        assigned_to=member_user,
        provider_name="Netflix 4K",
        category=BillCategory.OTHER,
        frequency=RecurrenceFrequency.MONTHLY,
        expected_amount=Decimal("649.00"),
        next_due_date=datetime.date(2026, 12, 1),
        is_active=True,
    )

    return {
        "b_oct": b_oct,
        "b_up_member": b_up_member,
        "b_up_owner": b_up_owner,
        "b_overdue": b_overdue,
        "payment": p1,
        "schedule": sched,
    }


@pytest.mark.django_db
class TestPhase7ControlledTools:
    def test_get_upcoming_bills_tool(self, test_household, member_user, owner_user, sample_financial_data):
        """Verifies get_upcoming_bills calculates correct totals and filters by assignee."""
        # All upcoming bills
        res_all = HouseholdAITools.get_upcoming_bills(test_household, member_user, days=30)
        assert res_all["upcoming_count"] == 2
        # 2000 (Tata Power) + 1200 (Airtel) = 3200.00
        assert res_all["total_upcoming_amount"] == "3200.00"

        # Only assigned to Member
        res_member = HouseholdAITools.get_upcoming_bills(test_household, member_user, days=30, only_assigned_to_me=True)
        assert res_member["upcoming_count"] == 1
        assert res_member["total_upcoming_amount"] == "2000.00"
        assert res_member["bills"][0]["provider_name"] == "Tata Power"

    def test_get_overdue_bills_tool(self, test_household, member_user, sample_financial_data):
        """Verifies get_overdue_bills identifies overdue items and calculates days overdue."""
        res = HouseholdAITools.get_overdue_bills(test_household, member_user)
        assert res["overdue_count"] == 1
        assert res["total_overdue_amount"] == "750.00"
        overdue_item = res["bills"][0]
        assert overdue_item["provider_name"] == "City Water Dept"
        assert overdue_item["days_overdue"] == 46

    def test_get_household_spending_tool(self, test_household, member_user, sample_financial_data):
        """Verifies get_household_spending calculates current month figures and comparisons."""
        res = HouseholdAITools.get_household_spending(test_household, member_user)
        assert res["period"] == "2026-11"
        # 2600 (Tata Power) + 1200 (Airtel) = 3800.00
        assert res["total_billed"] == "3800.00"
        assert res["total_paid"] == "600.00"
        assert res["total_outstanding"] == "3200.00"
        assert res["previous_total"] == "2000.00"  # b_oct (2000.00)

    def test_get_category_spending_tool(self, test_household, member_user, sample_financial_data):
        """Verifies get_category_spending calculates spend for Electricity."""
        res = HouseholdAITools.get_category_spending(test_household, member_user, category="ELECTRICITY", months=6)
        assert res["category"] == BillCategory.ELECTRICITY
        assert res["bills_count"] == 2
        # 2000 + 2600 = 4600.00
        assert res["total_spent"] == "4600.00"
        assert res["average_spent"] == "2300.00"

    def test_get_provider_spending_tool(self, test_household, member_user, sample_financial_data):
        """Verifies get_provider_spending calculates Tata Power spending and rate change."""
        res = HouseholdAITools.get_provider_spending(test_household, member_user, provider_name="Tata Power", months=6)
        assert res["bills_count"] == 2
        assert res["total_spent"] == "4600.00"
        assert "+30.0%" in res["percentage_change"]
        assert "increased" in res["rate_change_summary"]

    def test_get_payment_summary_tool(self, test_household, member_user, sample_financial_data):
        """Verifies get_payment_summary lists recent payments with receipt status."""
        res = HouseholdAITools.get_payment_summary(test_household, member_user)
        assert res["total_payments_count"] == 1
        assert res["total_amount_settled"] == "600.00"
        p = res["recent_payments"][0]
        assert p["provider_name"] == "Tata Power"
        assert p["transaction_reference"] == "UPI/2026/888999"

    def test_get_recurring_bills_tool(self, test_household, member_user, sample_financial_data):
        """Verifies get_recurring_bills calculates monthly equivalent recurring commitments."""
        res = HouseholdAITools.get_recurring_bills(test_household, member_user)
        assert res["active_schedules_count"] == 1
        assert res["estimated_monthly_recurring_total"] == "649.00"
        assert res["schedules"][0]["provider_name"] == "Netflix 4K"

    def test_security_outsider_cannot_call_tools_for_foreign_household(self, test_household, outsider_user, sample_financial_data):
        """Verifies that non-members receive permission error when calling tools."""
        res = HouseholdAITools.get_unpaid_bills(test_household, outsider_user)
        assert "error" in res
        assert "permission" in res["error"].lower()

    def test_tool_quantity_limit_parameter(self, test_household, member_user, sample_financial_data):
        """Verifies that the limit parameter restricts returned items to the specified count."""
        # 2 upcoming bills exist in sample_financial_data; limit=1 returns exactly 1 item
        res = HouseholdAITools.get_upcoming_bills(test_household, member_user, days=30, limit=1)
        assert len(res["bills"]) == 1
        assert res["total_count"] == 2
        assert res["limit_applied"] == 1


@pytest.mark.django_db
class TestPhase7ConversationalAssistant:
    def test_answer_upcoming_bills_assigned_to_me(self, test_household, member_user, sample_financial_data):
        """User asks: 'Show my upcoming bills.' -> Triggers get_upcoming_bills(only_assigned_to_me=True)."""
        conv = AIConversation.objects.create(household=test_household, user=member_user, title="Upcoming Bills Query")
        msg, tools = AIAssistantService.send_message(conv, member_user, "Show my upcoming bills.")

        assert len(tools) == 1
        assert tools[0].tool_name == "get_upcoming_bills"
        assert tools[0].tool_arguments.get("only_assigned_to_me") is True
        assert "2000.00 INR" in msg.content
        assert "Tata Power" in msg.content

    def test_answer_overdue_bills_question(self, test_household, member_user, sample_financial_data):
        """User asks: 'Which bills are overdue?' -> Triggers get_overdue_bills."""
        conv = AIConversation.objects.create(household=test_household, user=member_user)
        msg, tools = AIAssistantService.send_message(conv, member_user, "Which bills are overdue?")

        assert len(tools) == 1
        assert tools[0].tool_name == "get_overdue_bills"
        assert "750.00 INR" in msg.content
        assert "City Water Dept" in msg.content
        assert "46 days" in msg.content

    def test_assistant_respects_quantity_in_natural_language_prompt(self, test_household, member_user, sample_financial_data):
        """User asks: 'Show 1 upcoming bill' -> Passes limit=1 to tool and shows 1 item."""
        conv = AIConversation.objects.create(household=test_household, user=member_user)
        msg, tools = AIAssistantService.send_message(conv, member_user, "Show 1 upcoming bill")

        assert len(tools) == 1
        assert tools[0].tool_name == "get_upcoming_bills"
        assert tools[0].tool_arguments.get("limit") == 1
        assert "Showing **1 of 2 upcoming bills**" in msg.content

    def test_answer_electricity_spend_last_month(self, test_household, member_user, sample_financial_data):
        """User asks: 'How much did we spend on electricity last month?' -> Triggers get_category_spending."""
        conv = AIConversation.objects.create(household=test_household, user=member_user)
        msg, tools = AIAssistantService.send_message(conv, member_user, "How much did we spend on electricity last month?")

        assert len(tools) == 1
        assert tools[0].tool_name == "get_category_spending"
        assert "Electricity" in msg.content
        assert "INR" in msg.content

    def test_answer_monthly_expenses_summary(self, test_household, member_user, sample_financial_data):
        """User asks: 'Summarize this month\'s household expenses.' -> Triggers get_household_spending."""
        conv = AIConversation.objects.create(household=test_household, user=member_user)
        msg, tools = AIAssistantService.send_message(conv, member_user, "Summarize this month's household expenses.")

        assert len(tools) == 1
        assert tools[0].tool_name == "get_household_spending"
        assert "3800.00 INR" in msg.content
        assert "600.00 INR" in msg.content
        assert "3200.00 INR" in msg.content

    def test_answer_recurring_subscriptions_question(self, test_household, member_user, sample_financial_data):
        """User asks: 'What are our recurring subscriptions?' -> Triggers get_recurring_bills."""
        conv = AIConversation.objects.create(household=test_household, user=member_user)
        msg, tools = AIAssistantService.send_message(conv, member_user, "What are our recurring subscriptions?")

        assert len(tools) == 1
        assert tools[0].tool_name == "get_recurring_bills"
        assert "649.00 INR/month" in msg.content
        assert "Netflix 4K" in msg.content

    def test_tool_execution_audit_logging(self, test_household, member_user, sample_financial_data):
        """Verifies that tool calls and token usage are preserved in database tables."""
        conv = AIConversation.objects.create(household=test_household, user=member_user)
        msg, tools = AIAssistantService.send_message(conv, member_user, "Which bills are overdue?")

        # Check AIToolExecution row in DB
        db_exec = AIToolExecution.objects.filter(conversation=conv, tool_name="get_overdue_bills").first()
        assert db_exec is not None
        assert db_exec.is_success is True
        assert db_exec.execution_result["overdue_count"] == 1

        # Check AIUsageRecord row in DB
        db_usage = AIUsageRecord.objects.filter(household=test_household, user=member_user).first()
        assert db_usage is not None
        assert db_usage.total_tokens > 0


@pytest.mark.django_db
class TestPhase7APIAndWebInterface:
    def test_api_send_message_endpoint_and_isolation(self, member_user, outsider_user, test_household, sample_financial_data):
        """POST /api/v1/ai/conversations/{id}/send_message/ allows members and blocks outsiders."""
        conv = AIConversation.objects.create(household=test_household, user=member_user, title="API Chat")
        client = APIClient()

        # 1. Member sends message
        client.force_authenticate(user=member_user)
        res = client.post(f"/api/v1/ai/conversations/{conv.id}/send_message/", {"prompt": "Show my upcoming bills."})
        assert res.status_code == status.HTTP_200_OK
        assert "message" in res.data
        assert res.data["tool_executions_count"] >= 1

        # 2. Chronological messages endpoint
        res_msgs = client.get(f"/api/v1/ai/conversations/{conv.id}/messages/")
        assert res_msgs.status_code == status.HTTP_200_OK
        assert len(res_msgs.data) == 2  # 1 user + 1 assistant

        # 3. Outsider access is rejected (404 isolation)
        client.force_authenticate(user=outsider_user)
        res_outsider = client.post(f"/api/v1/ai/conversations/{conv.id}/send_message/", {"prompt": "Hack"})
        assert res_outsider.status_code == status.HTTP_404_NOT_FOUND

    def test_web_assistant_views_and_conversation_flow(self, member_user, test_household, sample_financial_data):
        """Tests the web UI assistant chat page, message sending, and new conversation creation."""
        client = APIClient()
        client.force_login(member_user)

        # 1. Access assistant page
        res = client.get(f"/assistant/?household={test_household.id}")
        assert res.status_code == status.HTTP_200_OK
        content = res.content.decode()
        assert "Household AI Assistant" in content
        assert "Strict Tool-Calling Guardrails Enabled" in content

        active_conv = AIConversation.objects.filter(household=test_household, user=member_user).first()
        assert active_conv is not None

        # 2. Send message via web form
        res_send = client.post(
            f"/assistant/conversations/{active_conv.id}/send/",
            {"prompt": "Which bills are overdue?"},
        )
        assert res_send.status_code == status.HTTP_302_FOUND

        # 3. Verify message is displayed with tool execution badge
        res_after = client.get(f"/assistant/conversations/{active_conv.id}/")
        assert res_after.status_code == status.HTTP_200_OK
        content_after = res_after.content.decode()
        assert "get_overdue_bills" in content_after
        assert "City Water Dept" in content_after

        # 4. Create new conversation via web form
        res_new = client.post("/assistant/new/", {"household_id": test_household.id})
        assert res_new.status_code == status.HTTP_302_FOUND
        assert AIConversation.objects.filter(household=test_household, user=member_user).count() >= 2

    def test_conversation_deletion_api_and_web_flow(self, member_user, owner_user, outsider_user, test_household):
        """Tests that conversation deletion is authorized, audited, and cleans up records."""
        from apps.audit.models import AuditEvent
        conv = AIConversation.objects.create(household=test_household, user=member_user, title="To Be Deleted")
        client = APIClient()

        # 1. Outsider cannot delete (404 isolation)
        client.force_authenticate(user=outsider_user)
        res_outsider = client.delete(f"/api/v1/ai/conversations/{conv.id}/")
        assert res_outsider.status_code == status.HTTP_404_NOT_FOUND

        # 2. Web deletion action works and logs audit
        client.force_login(member_user)
        res_web_del = client.post(f"/assistant/conversations/{conv.id}/delete/")
        assert res_web_del.status_code == status.HTTP_302_FOUND
        assert not AIConversation.objects.filter(id=conv.id).exists()

        # Verify audit log was created
        audit_entry = AuditEvent.objects.filter(
            action=AuditEvent.Action.CONVERSATION_DELETED,
            household=test_household,
        ).first()
        assert audit_entry is not None
        assert "To Be Deleted" in audit_entry.description
