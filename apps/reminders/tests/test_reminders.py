from datetime import timedelta
import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from django.core import mail
from rest_framework.test import APIClient
from rest_framework import status

from apps.households.models import Household, HouseholdMembership
from apps.bills.models import Bill, BillStatus
from apps.reminders.models import BillReminder, ReminderType, NotificationLog
from apps.reminders.services.reminder_service import ReminderService
from apps.reminders.tasks import (
    send_bill_reminder_task,
    process_daily_reminders,
    check_and_update_overdue_bills,
)

User = get_user_model()


@pytest.mark.django_db
class TestReminderAutomation:
    def setup_method(self):
        mail.outbox.clear()
        self.owner = User.objects.create_user(
            username="owner_remind",
            email="owner_remind@example.com",
            password="Password123!",
            first_name="Alice",
            last_name="Owner",
        )
        self.member = User.objects.create_user(
            username="member_remind",
            email="member_remind@example.com",
            password="Password123!",
            first_name="Bob",
            last_name="Member",
        )
        self.household = Household.objects.create(name="Reminder Home", created_by=self.owner)
        HouseholdMembership.objects.create(household=self.household, user=self.owner, role=HouseholdMembership.Role.OWNER)
        HouseholdMembership.objects.create(household=self.household, user=self.member, role=HouseholdMembership.Role.MEMBER)

    def test_reminder_dates_calculated_accurately(self):
        """Milestones are created for 3 days before, 1 day before, and on the due date."""
        today = timezone.localdate()
        due_date = today + timedelta(days=10)
        bill = Bill.objects.create(
            household=self.household,
            uploaded_by=self.owner,
            assigned_to=self.member,
            provider_name="City Power",
            amount="1500.00",
            due_date=due_date,
        )

        reminders = ReminderService.create_reminders_for_bill(bill)
        assert len(reminders) == 3

        types_and_dates = {r.reminder_type: r.remind_at for r in reminders}
        assert types_and_dates[ReminderType.THREE_DAYS_BEFORE] == due_date - timedelta(days=3)
        assert types_and_dates[ReminderType.ONE_DAY_BEFORE] == due_date - timedelta(days=1)
        assert types_and_dates[ReminderType.DUE_DATE] == due_date

        # Recipient is assigned member
        for r in reminders:
            assert r.sent_to == self.member

    def test_past_milestones_are_not_scheduled(self):
        """If a bill is due tomorrow, 3-day reminder is omitted."""
        today = timezone.localdate()
        due_date = today + timedelta(days=1)
        bill = Bill.objects.create(
            household=self.household,
            uploaded_by=self.owner,
            provider_name="Immediate Gas",
            amount="600.00",
            due_date=due_date,
        )

        reminders = ReminderService.create_reminders_for_bill(bill)
        # 3 days before is in the past, so only 1-day-before and due-date are created
        types = [r.reminder_type for r in reminders]
        assert ReminderType.THREE_DAYS_BEFORE not in types
        assert ReminderType.ONE_DAY_BEFORE in types
        assert ReminderType.DUE_DATE in types

    def test_duplicate_prevention(self):
        """Creating reminders repeatedly does not produce duplicate database records."""
        today = timezone.localdate()
        bill = Bill.objects.create(
            household=self.household,
            uploaded_by=self.owner,
            provider_name="Duplicate Test Electric",
            amount="2000.00",
            due_date=today + timedelta(days=5),
        )

        first_run = ReminderService.create_reminders_for_bill(bill)
        second_run = ReminderService.create_reminders_for_bill(bill)

        assert BillReminder.objects.filter(bill=bill).count() == 3

    def test_paid_bills_cancel_future_reminders(self):
        """Marking a bill as paid deletes all pending reminders."""
        today = timezone.localdate()
        bill = Bill.objects.create(
            household=self.household,
            uploaded_by=self.owner,
            provider_name="Water Bill",
            amount="400.00",
            due_date=today + timedelta(days=7),
        )
        ReminderService.create_reminders_for_bill(bill)
        assert BillReminder.objects.filter(bill=bill).count() == 3

        bill.mark_as_paid()
        # Pending reminders must be deleted
        assert BillReminder.objects.filter(bill=bill).count() == 0

    def test_send_bill_reminder_task_execution(self):
        """Executing reminder task dispatches email and creates log."""
        today = timezone.localdate()
        bill = Bill.objects.create(
            household=self.household,
            uploaded_by=self.owner,
            assigned_to=self.owner,
            provider_name="Fiber Broadband",
            amount="999.00",
            due_date=today + timedelta(days=3),
        )
        reminders = ReminderService.create_reminders_for_bill(bill)
        reminder = reminders[0]

        # Execute task
        result = send_bill_reminder_task(reminder.id)
        assert result is True

        reminder.refresh_from_db()
        assert reminder.is_sent is True
        assert reminder.sent_at is not None

        # Verify email was sent
        assert len(mail.outbox) == 1
        sent_email = mail.outbox[0]
        assert "Fiber Broadband" in sent_email.subject
        assert self.owner.email in sent_email.to

        # Verify audit log
        log = NotificationLog.objects.filter(reminder=reminder).first()
        assert log is not None
        assert log.status == NotificationLog.Status.SUCCESS

    def test_task_idempotency_prevents_duplicate_sends(self):
        """Running the same reminder task twice sends the email only once."""
        today = timezone.localdate()
        bill = Bill.objects.create(
            household=self.household,
            uploaded_by=self.owner,
            assigned_to=self.owner,
            provider_name="Idempotent Gas",
            amount="750.00",
            due_date=today + timedelta(days=3),
        )
        reminders = ReminderService.create_reminders_for_bill(bill)
        reminder = reminders[0]

        # First run -> sends email
        send_bill_reminder_task(reminder.id)
        assert len(mail.outbox) == 1

        # Second run -> skips duplicate
        send_bill_reminder_task(reminder.id)
        assert len(mail.outbox) == 1  # count remains 1

    def test_process_daily_reminders(self):
        """Process daily reminders task triggers all reminders due today or earlier."""
        today = timezone.localdate()
        bill = Bill.objects.create(
            household=self.household,
            uploaded_by=self.owner,
            assigned_to=self.owner,
            provider_name="Due Today Power",
            amount="1800.00",
            due_date=today,
        )
        reminders = ReminderService.create_reminders_for_bill(bill)
        assert len(reminders) == 1  # only due-date milestone since today is due-date

        dispatched = process_daily_reminders()
        assert dispatched == 1
        assert len(mail.outbox) == 1

    def test_check_and_update_overdue_bills(self):
        """Bills with past due dates are transitioned to OVERDUE and alert dispatched."""
        today = timezone.localdate()
        past_bill = Bill.objects.create(
            household=self.household,
            uploaded_by=self.owner,
            assigned_to=self.owner,
            provider_name="Overdue Utility",
            amount="2200.00",
            due_date=today - timedelta(days=2),
            status=BillStatus.PENDING,
        )

        updated_count = check_and_update_overdue_bills()
        assert updated_count == 1

        past_bill.refresh_from_db()
        assert past_bill.status == BillStatus.OVERDUE

        # Overdue reminder created and dispatched
        overdue_reminder = BillReminder.objects.filter(
            bill=past_bill,
            reminder_type=ReminderType.OVERDUE,
        ).first()
        assert overdue_reminder is not None
        assert overdue_reminder.is_sent is True
        assert len(mail.outbox) == 1
