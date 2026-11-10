import datetime
from decimal import Decimal
from unittest.mock import patch
import pytest
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.households.models import Household, HouseholdMembership
from apps.bills.models import Bill, BillCategory, BillStatus
from apps.ai.models import AIConversation, AIMessage
from apps.ai.services.assistant import AIAssistantService, _extract_quantity_limit
from apps.ai.templatetags.ai_markdown import render_markdown


@pytest.fixture(autouse=True)
def mock_localdate():
    with patch("django.utils.timezone.localdate", return_value=datetime.date(2026, 11, 10)):
        yield datetime.date(2026, 11, 10)


@pytest.fixture
def owner_user(db):
    return User.objects.create_user(
        username="owner_ai",
        email="owner_ai@example.com",
        password="Password123!",
        first_name="Alice",
        last_name="Owner",
    )


@pytest.fixture
def member_user(db):
    return User.objects.create_user(
        username="member_ai",
        email="member_ai@example.com",
        password="Password123!",
        first_name="Bob",
        last_name="Member",
    )


@pytest.fixture
def second_member_user(db):
    return User.objects.create_user(
        username="member2_ai",
        email="member2_ai@example.com",
        password="Password123!",
        first_name="Charlie",
        last_name="Member2",
    )


@pytest.fixture
def household(owner_user, member_user, second_member_user):
    hh = Household.objects.create(name="AI Test Household", created_by=owner_user)
    HouseholdMembership.objects.create(household=hh, user=owner_user, role=HouseholdMembership.Role.OWNER)
    HouseholdMembership.objects.create(household=hh, user=member_user, role=HouseholdMembership.Role.MEMBER)
    HouseholdMembership.objects.create(household=hh, user=second_member_user, role=HouseholdMembership.Role.MEMBER)
    return hh


@pytest.fixture
def sample_bills(household, owner_user, member_user):
    b1 = Bill.objects.create(
        household=household,
        uploaded_by=owner_user,
        assigned_to=member_user,
        provider_name="Tata Power",
        category=BillCategory.ELECTRICITY,
        amount=Decimal("2500.00"),
        total_paid=Decimal("500.00"),
        outstanding_balance=Decimal("2000.00"),
        status=BillStatus.PARTIALLY_PAID,
        due_date=datetime.date(2026, 11, 15),
    )
    b2 = Bill.objects.create(
        household=household,
        uploaded_by=owner_user,
        assigned_to=member_user,
        provider_name="Airtel Broadband",
        category=BillCategory.INTERNET,
        amount=Decimal("1200.00"),
        total_paid=Decimal("0.00"),
        outstanding_balance=Decimal("1200.00"),
        status=BillStatus.PENDING,
        due_date=datetime.date(2026, 11, 20),
    )
    b3 = Bill.objects.create(
        household=household,
        uploaded_by=owner_user,
        assigned_to=owner_user,
        provider_name="City Water",
        category=BillCategory.WATER,
        amount=Decimal("800.00"),
        total_paid=Decimal("0.00"),
        outstanding_balance=Decimal("800.00"),
        status=BillStatus.OVERDUE,
        due_date=datetime.date(2026, 10, 25),
    )
    return [b1, b2, b3]


@pytest.mark.django_db
class TestAIAssistantQuantityAndIntent:
    def test_extract_quantity_limit(self):
        assert _extract_quantity_limit("show me only any one unpaid bill") == 1
        assert _extract_quantity_limit("show just one bill") == 1
        assert _extract_quantity_limit("any one unpaid bill") == 1
        assert _extract_quantity_limit("first 3 bills") == 3
        assert _extract_quantity_limit("top 5 upcoming bills") == 5
        assert _extract_quantity_limit("show two bills") == 2
        assert _extract_quantity_limit("show all unpaid bills") is None
        assert _extract_quantity_limit("list every bill") is None

    def test_show_me_only_any_one_unpaid_bill(self, household, member_user, sample_bills):
        conv = AIConversation.objects.create(household=household, user=member_user, title="Quantity Test")
        msg, tool_execs = AIAssistantService.send_message(
            conversation=conv,
            user=member_user,
            prompt="show me only any one unpaid bill",
        )
        assert len(tool_execs) == 1
        tool_exec = tool_execs[0]
        assert tool_exec.tool_name == "get_unpaid_bills"
        assert tool_exec.tool_arguments.get("limit") == 1
        result = tool_exec.execution_result
        assert len(result["bills"]) == 1
        assert result["limit_applied"] == 1
        # The content reflects 1 bill shown and total outstanding preserved
        assert "1 of 3 unpaid bills" in msg.content
        assert "4000.00" in msg.content

    def test_what_is_the_weather_does_not_call_unpaid_bills(self, household, member_user, sample_bills):
        conv = AIConversation.objects.create(household=household, user=member_user, title="Weather Test")
        msg, tool_execs = AIAssistantService.send_message(
            conversation=conv,
            user=member_user,
            prompt="What is the weather today?",
        )
        # Should not execute get_unpaid_bills
        assert len(tool_execs) == 0
        assert "outside my supported capabilities" in msg.content
        assert "Tata Power" not in msg.content

    def test_how_much_is_outstanding(self, household, member_user, sample_bills):
        conv = AIConversation.objects.create(household=household, user=member_user, title="Outstanding Test")
        msg, tool_execs = AIAssistantService.send_message(
            conversation=conv,
            user=member_user,
            prompt="How much is outstanding?",
        )
        assert len(tool_execs) == 1
        assert tool_execs[0].tool_name == "get_unpaid_bills"
        # Total outstanding: 2000 + 1200 + 800 = 4000.00
        assert "4000" in msg.content
        assert "Total Outstanding" in msg.content or "outstanding" in msg.content.lower()

    def test_explain_my_electricity_bill(self, household, member_user, sample_bills):
        conv = AIConversation.objects.create(household=household, user=member_user, title="Explain Test")
        msg, tool_execs = AIAssistantService.send_message(
            conversation=conv,
            user=member_user,
            prompt="Explain my electricity bill",
        )
        assert len(tool_execs) == 1
        assert tool_execs[0].tool_name == "explain_bill"
        assert "Tata Power" in msg.content
        assert "2000" in msg.content


@pytest.mark.django_db
class TestConversationOwnershipAndPermissions:
    def test_member_cannot_read_or_message_other_member_conversation_api(
        self, household, member_user, second_member_user
    ):
        # Conversation owned by member_user
        conv = AIConversation.objects.create(
            household=household,
            user=member_user,
            title="Member 1 Secret Chat",
        )

        client = APIClient()
        client.force_authenticate(user=second_member_user)

        # 1. Listing should not show member_user's conversation to second_member_user
        res_list = client.get("/api/v1/ai/conversations/")
        assert res_list.status_code == status.HTTP_200_OK
        ids = [c["id"] for c in res_list.data["results"]]
        assert conv.id not in ids

        # 2. Detail retrieve should return 404 for second_member_user
        res_detail = client.get(f"/api/v1/ai/conversations/{conv.id}/")
        assert res_detail.status_code == status.HTTP_404_NOT_FOUND

        # 3. Sending message should return 404 for second_member_user
        res_send = client.post(
            f"/api/v1/ai/conversations/{conv.id}/send_message/",
            {"prompt": "Intruder prompt"},
        )
        assert res_send.status_code == status.HTTP_404_NOT_FOUND

    def test_owner_can_access_member_conversation_api(
        self, household, member_user, owner_user
    ):
        conv = AIConversation.objects.create(
            household=household,
            user=member_user,
            title="Member 1 Chat",
        )
        client = APIClient()
        client.force_authenticate(user=owner_user)

        # Owner has administrative visibility over household conversations
        res_list = client.get(f"/api/v1/ai/conversations/?household={household.id}")
        assert res_list.status_code == status.HTTP_200_OK
        ids = [c["id"] for c in res_list.data["results"]]
        assert conv.id in ids

        res_detail = client.get(f"/api/v1/ai/conversations/{conv.id}/")
        assert res_detail.status_code == status.HTTP_200_OK

    def test_web_view_blocks_unauthorized_member_access(
        self, household, member_user, second_member_user
    ):
        conv = AIConversation.objects.create(
            household=household,
            user=member_user,
            title="Member 1 Chat",
        )
        client = APIClient()
        client.force_login(second_member_user)

        # Accessing member_user's conversation URL
        res = client.get(f"/assistant/conversations/{conv.id}/")
        # Should redirect with error message
        assert res.status_code == status.HTTP_302_FOUND
        assert res.url == "/assistant/"

        # Posting message to member_user's conversation
        res_post = client.post(
            f"/assistant/conversations/{conv.id}/send/",
            {"prompt": "Unauthorized message"},
        )
        assert res_post.status_code == status.HTTP_302_FOUND
        assert res_post.url == "/assistant/"


class TestMarkdownRendering:
    def test_markdown_filter_escapes_xss_and_renders_markdown(self):
        unsafe_markdown = "### Heading\n\n**Bold text**\n\n<script>alert('xss')</script>\n\n- Item 1\n- Item 2"
        rendered = render_markdown(unsafe_markdown)
        # HTML tag should be escaped
        assert "<script>" not in rendered
        assert "&lt;script&gt;" in rendered
        assert "&lt;/script&gt;" in rendered
        # Markdown should be parsed into HTML
        assert "<h3>Heading</h3>" in rendered
        assert "<strong>Bold text</strong>" in rendered
        assert "<li>Item 1</li>" in rendered
