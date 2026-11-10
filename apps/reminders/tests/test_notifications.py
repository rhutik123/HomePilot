import pytest
from datetime import timedelta
from decimal import Decimal
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.households.models import Household, HouseholdMembership
from apps.bills.models import Bill, BillCategory, BillStatus
from apps.reminders.models import (
    BillReminder,
    ReminderType,
    NotificationPreference,
    InAppNotification,
    NotificationDelivery,
    NotificationChannel,
)
from django.test import override_settings
from apps.reminders.services.dispatcher import NotificationDispatcherService
from apps.reminders.services.channels.whatsapp import WhatsAppChannel
from apps.reminders.tasks import send_bill_reminder_task


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def test_user(db):
    return User.objects.create_user(
        username="notify_user",
        email="notify_user@example.com",
        password="Password123!",
        first_name="Notification",
        last_name="Tester",
    )


@pytest.fixture
def household(test_user):
    hh = Household.objects.create(name="Palm Villa", created_by=test_user)
    HouseholdMembership.objects.create(household=hh, user=test_user, role=HouseholdMembership.Role.OWNER)
    return hh


@pytest.fixture
def sample_bill(household, test_user):
    today = timezone.localdate()
    return Bill.objects.create(
        household=household,
        uploaded_by=test_user,
        assigned_to=test_user,
        provider_name="Green Energy Co",
        category=BillCategory.ELECTRICITY,
        amount=Decimal("2500.00"),
        currency="INR",
        due_date=today + timedelta(days=3),
    )


@pytest.fixture
def sample_reminder(sample_bill, test_user):
    today = timezone.localdate()
    return BillReminder.objects.create(
        bill=sample_bill,
        reminder_type=ReminderType.THREE_DAYS_BEFORE,
        remind_at=today,
        sent_to=test_user,
        is_sent=False,
    )


@pytest.mark.django_db
class TestNotificationPreferencesAPI:
    def test_get_and_update_preferences(self, api_client, test_user):
        api_client.force_authenticate(user=test_user)
        # GET creates default if not existing
        res = api_client.get("/api/v1/reminders/preferences/")
        assert res.status_code == status.HTTP_200_OK
        assert res.data["email_enabled"] is True
        assert res.data["in_app_enabled"] is True
        assert res.data["whatsapp_enabled"] is False

        # PATCH to enable WhatsApp with valid phone
        res_patch = api_client.patch(
            "/api/v1/reminders/preferences/",
            {
                "whatsapp_enabled": True,
                "whatsapp_phone_number": "+919876543210",
            },
        )
        assert res_patch.status_code == status.HTTP_200_OK
        assert res_patch.data["whatsapp_enabled"] is True
        assert res_patch.data["whatsapp_phone_number"] == "+919876543210"

    def test_invalid_whatsapp_phone_rejection(self, api_client, test_user):
        api_client.force_authenticate(user=test_user)
        res = api_client.patch(
            "/api/v1/reminders/preferences/",
            {"whatsapp_phone_number": "not-a-number"},
        )
        assert res.status_code == status.HTTP_400_BAD_REQUEST
        assert "whatsapp_phone_number" in res.data


@pytest.mark.django_db
class TestMultiChannelDispatcher:
    def test_whatsapp_phone_validator(self):
        assert WhatsAppChannel.validate_phone_number("+919876543210") is True
        assert WhatsAppChannel.validate_phone_number("+14155552671") is True
        assert WhatsAppChannel.validate_phone_number("123") is False
        assert WhatsAppChannel.validate_phone_number("abc") is False
        assert WhatsAppChannel.validate_phone_number(None) is False

    @override_settings(WHATSAPP_SIMULATION_ENABLED=True)
    def test_dispatcher_creates_in_app_and_audit_deliveries(self, sample_reminder, test_user):
        # Enable all 3 channels
        prefs, _ = NotificationPreference.objects.get_or_create(user=test_user)
        prefs.email_enabled = True
        prefs.in_app_enabled = True
        prefs.whatsapp_enabled = True
        prefs.whatsapp_phone_number = "+919876543210"
        prefs.save()

        result = NotificationDispatcherService.dispatch(sample_reminder)
        assert result["success"] is True
        assert "email" in result["channels"]
        assert "in_app" in result["channels"]
        assert "whatsapp" in result["channels"]

        # Check InAppNotification was created
        in_app_alerts = InAppNotification.objects.filter(user=test_user)
        assert in_app_alerts.count() == 1
        assert "Green Energy Co" in in_app_alerts.first().title

        # Check NotificationDelivery records created for all 3 channels
        deliveries = NotificationDelivery.objects.filter(reminder=sample_reminder)
        assert deliveries.count() == 3
        channels = set(deliveries.values_list("channel", flat=True))
        assert channels == {"EMAIL", "IN_APP", "WHATSAPP"}

    def test_send_bill_reminder_task_flow(self, sample_reminder):
        # Celery eager task
        res = send_bill_reminder_task(sample_reminder.id)
        assert res is True

        sample_reminder.refresh_from_db()
        assert sample_reminder.is_sent is True
        assert sample_reminder.sent_at is not None

        # Repeat call is idempotent (skips already sent reminder)
        res_repeat = send_bill_reminder_task(sample_reminder.id)
        assert res_repeat is True


@pytest.mark.django_db
class TestInAppNotificationInboxAPI:
    def test_inbox_list_and_mark_read(self, api_client, test_user, sample_bill, sample_reminder):
        InAppNotification.objects.create(
            user=test_user,
            bill=sample_bill,
            reminder=sample_reminder,
            title="Alert 1",
            message="Test message 1",
            is_read=False,
        )
        InAppNotification.objects.create(
            user=test_user,
            bill=sample_bill,
            reminder=sample_reminder,
            title="Alert 2",
            message="Test message 2",
            is_read=False,
        )

        api_client.force_authenticate(user=test_user)
        res = api_client.get("/api/v1/reminders/inbox/")
        assert res.status_code == status.HTTP_200_OK
        assert len(res.data["results"]) == 2

        # Mark all as read
        res_mark_all = api_client.post("/api/v1/reminders/inbox/mark_all_read/")
        assert res_mark_all.status_code == status.HTTP_200_OK
        assert res_mark_all.data["count"] == 2

        # Now 0 unread
        res_unread = api_client.get("/api/v1/reminders/inbox/?is_read=false")
        assert len(res_unread.data["results"]) == 0
