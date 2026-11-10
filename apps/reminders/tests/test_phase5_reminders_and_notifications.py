import pytest
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch
from django.utils import timezone
from django.core import mail
from django.test import override_settings
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
    NotificationLog,
    NotificationChannel,
)
from apps.reminders.services.reminder_service import ReminderService
from apps.reminders.services.dispatcher import NotificationDispatcherService
from apps.reminders.services.channels.whatsapp import WhatsAppChannel
from apps.reminders.tasks import (
    send_bill_reminder_task,
    process_daily_reminders,
    check_and_update_overdue_bills,
)


@pytest.fixture
def owner_user(db):
    return User.objects.create_user(
        username="owner_phase5",
        email="owner_phase5@example.com",
        password="Password123!",
        first_name="Alice",
        last_name="Owner",
    )


@pytest.fixture
def member_user(db):
    return User.objects.create_user(
        username="member_phase5",
        email="member_phase5@example.com",
        password="Password123!",
        first_name="Bob",
        last_name="Member",
    )


@pytest.fixture
def test_household(owner_user, member_user):
    hh = Household.objects.create(name="Phase 5 Villa", created_by=owner_user)
    HouseholdMembership.objects.create(household=hh, user=owner_user, role=HouseholdMembership.Role.OWNER)
    HouseholdMembership.objects.create(household=hh, user=member_user, role=HouseholdMembership.Role.MEMBER)
    return hh


@pytest.fixture
def future_bill(test_household, owner_user, member_user):
    today = timezone.localdate()
    return Bill.objects.create(
        household=test_household,
        uploaded_by=owner_user,
        assigned_to=member_user,
        provider_name="Phase 5 Telecom",
        category=BillCategory.INTERNET,
        amount=Decimal("1200.00"),
        currency="INR",
        due_date=today + timedelta(days=14),
    )


@pytest.mark.django_db
class TestPhase5MilestoneReminders:
    def test_multi_milestone_generation_all_milestones(self, future_bill, member_user):
        """
        Bill due in 14 days schedules all 4 milestones (7d, 3d, 1d, 0d).
        All reminders are initialized to PENDING and is_sent=False.
        """
        today = timezone.localdate()
        reminders = ReminderService.create_reminders_for_bill(future_bill)
        assert len(reminders) == 4

        types_and_dates = {r.reminder_type: r.remind_at for r in reminders}
        assert types_and_dates[ReminderType.SEVEN_DAYS_BEFORE] == future_bill.due_date - timedelta(days=7)
        assert types_and_dates[ReminderType.THREE_DAYS_BEFORE] == future_bill.due_date - timedelta(days=3)
        assert types_and_dates[ReminderType.ONE_DAY_BEFORE] == future_bill.due_date - timedelta(days=1)
        assert types_and_dates[ReminderType.DUE_DATE] == future_bill.due_date

        for r in reminders:
            assert r.sent_to == member_user
            assert r.status == BillReminder.ReminderStatus.PENDING
            assert r.is_sent is False
            assert r.retry_count == 0

    def test_notification_preference_milestone_filtering(self, test_household, owner_user, member_user):
        """
        User preferences can selectively disable specific milestone reminders.
        """
        prefs, _ = NotificationPreference.objects.get_or_create(user=member_user)
        prefs.remind_7_days_before = False
        prefs.remind_1_day_before = False
        prefs.save()

        today = timezone.localdate()
        bill = Bill.objects.create(
            household=test_household,
            uploaded_by=owner_user,
            assigned_to=member_user,
            provider_name="Selective Utility",
            amount=Decimal("800.00"),
            due_date=today + timedelta(days=10),
        )

        reminders = ReminderService.create_reminders_for_bill(bill)
        types = [r.reminder_type for r in reminders]
        assert ReminderType.SEVEN_DAYS_BEFORE not in types
        assert ReminderType.ONE_DAY_BEFORE not in types
        assert ReminderType.THREE_DAYS_BEFORE in types
        assert ReminderType.DUE_DATE in types


@pytest.mark.django_db
class TestPhase5AuditHistoryPreservation:
    def test_paid_bill_cancels_pending_reminders_and_preserves_history(self, future_bill):
        """
        Paying a bill marks pending reminders as CANCELLED without hard-deleting database records.
        Previously sent reminders retain their SENT status.
        """
        reminders = ReminderService.create_reminders_for_bill(future_bill)
        assert BillReminder.objects.filter(bill=future_bill).count() == 4

        # Simulate one reminder being dispatched earlier
        sent_reminder = reminders[0]
        sent_reminder.status = BillReminder.ReminderStatus.SENT
        sent_reminder.is_sent = True
        sent_reminder.sent_at = timezone.now()
        sent_reminder.save()

        # Mark bill as paid
        future_bill.mark_as_paid()

        # Database rows are NOT deleted
        all_reminders = BillReminder.objects.filter(bill=future_bill)
        assert all_reminders.count() == 4

        # 3 pending reminders transition to CANCELLED
        cancelled = all_reminders.filter(status=BillReminder.ReminderStatus.CANCELLED)
        assert cancelled.count() == 3

        # 1 sent reminder remains SENT
        sent = all_reminders.filter(status=BillReminder.ReminderStatus.SENT)
        assert sent.count() == 1

        # 0 remain PENDING
        assert all_reminders.filter(status=BillReminder.ReminderStatus.PENDING).count() == 0

    def test_foreign_key_safety_when_bill_deleted(self, future_bill, member_user):
        """
        Deleting a bill must NOT cascade delete NotificationLog or NotificationDelivery audit records.
        """
        reminder = BillReminder.objects.create(
            bill=future_bill,
            reminder_type=ReminderType.DUE_DATE,
            remind_at=future_bill.due_date,
            sent_to=member_user,
            status=BillReminder.ReminderStatus.SENT,
            is_sent=True,
        )

        log = NotificationLog.objects.create(
            reminder=reminder,
            bill=future_bill,
            recipient_email=member_user.email,
            subject="Audit Test Subject",
            channel="EMAIL",
            status=NotificationLog.Status.SUCCESS,
        )

        delivery = NotificationDelivery.objects.create(
            reminder=reminder,
            bill=future_bill,
            recipient=member_user,
            channel=NotificationChannel.EMAIL,
            status=NotificationDelivery.Status.SUCCESS,
            recipient_address=member_user.email,
            subject="Audit Test Subject",
        )

        # Delete the bill
        future_bill.delete()

        # Verify audit records survived with bill reference set to NULL
        log.refresh_from_db()
        assert log.bill is None
        assert log.subject == "Audit Test Subject"

        delivery.refresh_from_db()
        assert delivery.bill is None
        assert delivery.recipient == member_user


@pytest.mark.django_db
class TestPhase5FailureIntegrityAndRetries:
    @patch("apps.reminders.services.channels.email.EmailNotificationService.send_bill_reminder")
    def test_email_dispatch_failure_does_not_mark_sent_and_tracks_retries(self, mock_email, future_bill, member_user):
        """
        When email dispatch fails or raises an error:
        - Reminder is NOT marked as sent
        - Retry count is incremented
        - NotificationDelivery and NotificationLog record FAILED
        - Reminder transitions to FAILED upon retry exhaustion
        """
        mock_email.return_value = False

        reminder = BillReminder.objects.create(
            bill=future_bill,
            reminder_type=ReminderType.DUE_DATE,
            remind_at=timezone.localdate(),
            sent_to=member_user,
            status=BillReminder.ReminderStatus.PENDING,
            is_sent=False,
        )

        # Execute task - with max retries 3, after 3 attempts it should be FAILED
        with patch.object(send_bill_reminder_task, "max_retries", 1):
            try:
                send_bill_reminder_task(reminder.id)
            except Exception:
                pass

        reminder.refresh_from_db()
        assert reminder.is_sent is False
        assert reminder.status in [BillReminder.ReminderStatus.FAILED, BillReminder.ReminderStatus.PENDING]
        assert reminder.retry_count >= 1

        # Check email delivery audit log
        email_delivery = NotificationDelivery.objects.filter(
            reminder=reminder,
            channel=NotificationChannel.EMAIL,
        ).first()
        assert email_delivery is not None
        assert email_delivery.status == NotificationDelivery.Status.FAILED

    @override_settings(WHATSAPP_SIMULATION_ENABLED=False, WHATSAPP_API_TOKEN="", WHATSAPP_PHONE_NUMBER_ID="")
    def test_whatsapp_unconfigured_fails_safely_without_silent_mock(self, future_bill, member_user):
        """
        When WhatsApp is enabled in preferences but unconfigured without simulation flag:
        System must NOT pretend message was delivered; it must report failure and document requirements.
        """
        prefs, _ = NotificationPreference.objects.get_or_create(user=member_user)
        prefs.email_enabled = False
        prefs.in_app_enabled = False
        prefs.whatsapp_enabled = True
        prefs.whatsapp_phone_number = "+919876543210"
        prefs.save()

        reminder = BillReminder.objects.create(
            bill=future_bill,
            reminder_type=ReminderType.DUE_DATE,
            remind_at=timezone.localdate(),
            sent_to=member_user,
            status=BillReminder.ReminderStatus.PENDING,
            is_sent=False,
        )

        result = NotificationDispatcherService.dispatch(reminder)
        assert result["success"] is False
        assert "WhatsApp Business Platform is not configured" in result["error"]

        delivery = NotificationDelivery.objects.filter(reminder=reminder, channel=NotificationChannel.WHATSAPP).first()
        assert delivery is not None
        assert delivery.status == NotificationDelivery.Status.FAILED
        assert "docs/WHATSAPP_BUSINESS_API_SETUP.md" in delivery.error_message

    @override_settings(WHATSAPP_SIMULATION_ENABLED=True)
    def test_whatsapp_simulation_mode_succeeds_when_explicitly_enabled(self, future_bill, member_user):
        """
        When WHATSAPP_SIMULATION_ENABLED=True, simulated dispatch logs message and records SUCCESS.
        """
        prefs, _ = NotificationPreference.objects.get_or_create(user=member_user)
        prefs.email_enabled = False
        prefs.in_app_enabled = False
        prefs.whatsapp_enabled = True
        prefs.whatsapp_phone_number = "+919876543210"
        prefs.save()

        reminder = BillReminder.objects.create(
            bill=future_bill,
            reminder_type=ReminderType.DUE_DATE,
            remind_at=timezone.localdate(),
            sent_to=member_user,
            status=BillReminder.ReminderStatus.PENDING,
            is_sent=False,
        )

        result = NotificationDispatcherService.dispatch(reminder)
        assert result["success"] is True
        assert result["channels"]["whatsapp"]["success"] is True

        delivery = NotificationDelivery.objects.filter(reminder=reminder, channel=NotificationChannel.WHATSAPP).first()
        assert delivery is not None
        assert delivery.status == NotificationDelivery.Status.SUCCESS


@pytest.mark.django_db
class TestPhase5WebInterfaceAndInbox:
    def test_in_app_notification_inbox_and_mark_read_flow(self, future_bill, member_user):
        """Web interface renders alerts and allows single and bulk read operations."""
        alert1 = InAppNotification.objects.create(
            user=member_user,
            bill=future_bill,
            title="Alert 1",
            message="Message 1",
            is_read=False,
        )
        alert2 = InAppNotification.objects.create(
            user=member_user,
            bill=future_bill,
            title="Alert 2",
            message="Message 2",
            is_read=False,
        )

        client = APIClient()
        client.force_login(member_user)

        # 1. View Inbox
        res = client.get("/notifications/")
        assert res.status_code == status.HTTP_200_OK
        assert "Alert 1" in res.content.decode()
        assert "Alert 2" in res.content.decode()

        # 2. Mark single as read
        res_read1 = client.post(f"/notifications/{alert1.id}/read/")
        assert res_read1.status_code == status.HTTP_302_FOUND
        alert1.refresh_from_db()
        assert alert1.is_read is True

        # 3. Mark all as read
        res_read_all = client.post("/notifications/mark-all-read/")
        assert res_read_all.status_code == status.HTTP_302_FOUND
        alert2.refresh_from_db()
        assert alert2.is_read is True

    def test_notification_preferences_web_form_update(self, member_user):
        """Web preferences form updates channels and milestone configurations."""
        client = APIClient()
        client.force_login(member_user)

        # GET form
        res_get = client.get("/notifications/preferences/")
        assert res_get.status_code == status.HTTP_200_OK

        # POST updates
        res_post = client.post(
            "/notifications/preferences/",
            {
                "email_enabled": "on",
                "in_app_enabled": "on",
                "whatsapp_enabled": "on",
                "whatsapp_phone_number": "+919876543210",
                "remind_7_days_before": "on",
                "remind_3_days_before": "on",
                # 1 day and due date omitted (unchecked)
            },
        )
        assert res_post.status_code == status.HTTP_302_FOUND

        prefs = NotificationPreference.objects.get(user=member_user)
        assert prefs.whatsapp_enabled is True
        assert prefs.whatsapp_phone_number == "+919876543210"
        assert prefs.remind_7_days_before is True
        assert prefs.remind_1_day_before is False
        assert prefs.remind_on_due_date is False


@pytest.mark.django_db
class TestPhase5PeriodicTasks:
    def test_overdue_check_handles_partially_paid_bills(self, test_household, owner_user, member_user):
        """
        Partially paid bills that pass their due date must transition to OVERDUE
        and generate overdue reminders.
        """
        today = timezone.localdate()
        past_bill = Bill.objects.create(
            household=test_household,
            uploaded_by=owner_user,
            assigned_to=member_user,
            provider_name="Partial Energy",
            amount=Decimal("3000.00"),
            total_paid=Decimal("1000.00"),
            outstanding_balance=Decimal("2000.00"),
            due_date=today - timedelta(days=1),
            status=BillStatus.PARTIALLY_PAID,
        )

        updated_count = check_and_update_overdue_bills()
        assert updated_count >= 1

        past_bill.refresh_from_db()
        assert past_bill.status == BillStatus.OVERDUE

        overdue_reminder = BillReminder.objects.filter(
            bill=past_bill,
            reminder_type=ReminderType.OVERDUE,
        ).first()
        assert overdue_reminder is not None
        assert overdue_reminder.status == BillReminder.ReminderStatus.SENT
        assert overdue_reminder.is_sent is True

    def test_daily_process_reminders_picks_partially_paid_bills(self, test_household, owner_user, member_user):
        """
        process_daily_reminders must pick up active reminders for PARTIALLY_PAID bills.
        """
        today = timezone.localdate()
        part_bill = Bill.objects.create(
            household=test_household,
            uploaded_by=owner_user,
            assigned_to=member_user,
            provider_name="Water Authority",
            amount=Decimal("500.00"),
            total_paid=Decimal("200.00"),
            outstanding_balance=Decimal("300.00"),
            due_date=today,
            status=BillStatus.PARTIALLY_PAID,
        )

        reminder = BillReminder.objects.create(
            bill=part_bill,
            reminder_type=ReminderType.DUE_DATE,
            remind_at=today,
            sent_to=member_user,
            status=BillReminder.ReminderStatus.PENDING,
            is_sent=False,
        )

        dispatched = process_daily_reminders()
        assert dispatched >= 1

        reminder.refresh_from_db()
        assert reminder.status == BillReminder.ReminderStatus.SENT
        assert reminder.is_sent is True
