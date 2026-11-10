from datetime import date, timedelta
from decimal import Decimal
import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework import status

from apps.households.models import Household, HouseholdMembership
from apps.bills.models import Bill, BillStatus, BillCategory
from apps.bills.services.bill_service import BillService
from apps.recurring.models import RecurringSchedule, RecurringOccurrence, RecurrenceFrequency
from apps.recurring.services.schedule_service import (
    RecurringScheduleService,
    calculate_next_due_date,
    get_period_identifier,
)
from apps.recurring.tasks import generate_recurring_bills_task

User = get_user_model()


@pytest.mark.django_db
class TestPhase4RecurringBills:
    def setup_method(self):
        self.client = APIClient()
        self.owner = User.objects.create_user(
            username="p4_owner",
            email="p4_owner@example.com",
            password="SecurePassword123!",
            first_name="Alice",
            last_name="Owner",
        )
        self.member = User.objects.create_user(
            username="p4_member",
            email="p4_member@example.com",
            password="SecurePassword123!",
            first_name="Bob",
            last_name="Member",
        )
        self.stranger = User.objects.create_user(
            username="p4_stranger",
            email="p4_stranger@example.com",
            password="SecurePassword123!",
        )

        self.household = Household.objects.create(name="Phase 4 Household", created_by=self.owner)
        HouseholdMembership.objects.create(household=self.household, user=self.owner, role=HouseholdMembership.Role.OWNER)
        HouseholdMembership.objects.create(household=self.household, user=self.member, role=HouseholdMembership.Role.MEMBER)

        self.client.force_login(self.owner)
        session = self.client.session
        session["active_household_id"] = self.household.id
        session.save()

    def test_create_and_update_recurring_schedule_service(self):
        """Creates and updates recurring schedule via service with validation and audit logging."""
        today = timezone.localdate()
        target_due = today + timedelta(days=10)

        schedule = RecurringScheduleService.create_schedule(
            household=self.household,
            created_by=self.owner,
            provider_name="Tata Power Mumbai",
            category=BillCategory.ELECTRICITY,
            expected_amount=Decimal("3200.00"),
            frequency=RecurrenceFrequency.MONTHLY,
            next_due_date=target_due,
            day_of_month=target_due.day,
            assigned_to=self.member,
            notes="Residential monthly electricity estimate",
        )
        assert schedule.id is not None
        assert schedule.provider_name == "Tata Power Mumbai"
        assert schedule.expected_amount == Decimal("3200.00")
        assert schedule.assigned_to == self.member
        assert schedule.is_active is True

        # Update schedule parameters (amount and day)
        updated = RecurringScheduleService.update_schedule(
            schedule=schedule,
            user=self.owner,
            expected_amount=Decimal("3500.00"),
            notes="Tariff revised upwards",
        )
        assert updated.expected_amount == Decimal("3500.00")
        assert updated.notes == "Tariff revised upwards"

    def test_pause_and_resume_schedule_service(self):
        """Pausing schedule stops automatic generation; resuming reactivates it."""
        today = timezone.localdate()
        schedule = RecurringScheduleService.create_schedule(
            household=self.household,
            created_by=self.owner,
            provider_name="Gym Membership",
            category=BillCategory.OTHER,
            expected_amount=Decimal("2000.00"),
            next_due_date=today + timedelta(days=3),
        )

        RecurringScheduleService.pause_schedule(schedule, self.owner)
        schedule.refresh_from_db()
        assert schedule.is_active is False

        # Paused schedules are skipped by generator
        pairs = RecurringScheduleService.generate_due_occurrences(generate_ahead_days=5)
        assert len(pairs) == 0

        # Resume schedule
        RecurringScheduleService.resume_schedule(schedule, self.owner)
        schedule.refresh_from_db()
        assert schedule.is_active is True

        # Now generator processes it
        pairs_active = RecurringScheduleService.generate_due_occurrences(generate_ahead_days=5)
        assert len(pairs_active) == 1
        assert pairs_active[0][0].id == schedule.id

    def test_detect_missed_schedules_and_catch_up(self):
        """Detects missed past recurring cycles and generates catch-up bill occurrences."""
        today = timezone.localdate()
        # Schedule was due 40 days ago
        past_due = today - timedelta(days=40)

        schedule = RecurringSchedule.objects.create(
            household=self.household,
            created_by=self.owner,
            provider_name="Missed Fiber Broadband",
            category=BillCategory.INTERNET,
            expected_amount=Decimal("1299.00"),
            frequency=RecurrenceFrequency.MONTHLY,
            day_of_month=past_due.day,
            next_due_date=past_due,
            is_active=True,
        )

        # 1. Detection
        missed = RecurringScheduleService.detect_missed_schedules(self.household)
        assert len(missed) == 1
        assert missed[0]["schedule"].id == schedule.id
        assert missed[0]["days_overdue"] >= 40

        # 2. Catch up missed cycles
        catch_up_bills = RecurringScheduleService.catch_up_missed_occurrences(schedule, self.owner)
        assert len(catch_up_bills) >= 1
        for b in catch_up_bills:
            assert b.status in (BillStatus.PENDING, BillStatus.OVERDUE)
            assert "Missed Fiber Broadband" in b.provider_name

        schedule.refresh_from_db()
        # Next due date has caught up to the present/future
        assert schedule.next_due_date >= today

        # Detection now returns empty
        missed_after = RecurringScheduleService.detect_missed_schedules(self.household)
        assert len(missed_after) == 0

    def test_actual_bill_amount_can_differ_from_expected_schedule_amount(self):
        """Requirement 8: Actual bill amount can be modified without altering schedule expected baseline."""
        today = timezone.localdate()
        schedule = RecurringScheduleService.create_schedule(
            household=self.household,
            created_by=self.owner,
            provider_name="City Gas Utility",
            category=BillCategory.GAS,
            expected_amount=Decimal("1000.00"),
            next_due_date=today + timedelta(days=5),
        )

        _, bill = RecurringScheduleService.trigger_occurrence(schedule, self.owner)
        assert bill.amount == Decimal("1000.00")
        assert bill.outstanding_balance == Decimal("1000.00")

        # Actual gas bill arrived with higher consumption: ₹1,450.00
        BillService.update_bill(
            bill=bill,
            user=self.owner,
            amount="1450.00",
            notes="Meter reading verified: 145 units",
        )

        bill.refresh_from_db()
        assert bill.amount == Decimal("1450.00")
        assert bill.outstanding_balance == Decimal("1450.00")

        # Baseline expected amount on schedule remains intact
        schedule.refresh_from_db()
        assert schedule.expected_amount == Decimal("1000.00")

    def test_schedule_change_does_not_regenerate_past_occurrences(self):
        """Requirement 10: Modifying schedule parameters does not generate duplicate bills for past periods."""
        today = timezone.localdate()
        schedule = RecurringScheduleService.create_schedule(
            household=self.household,
            created_by=self.owner,
            provider_name="Cloud Storage Subscription",
            category=BillCategory.OTHER,
            expected_amount=Decimal("650.00"),
            frequency=RecurrenceFrequency.MONTHLY,
            next_due_date=today,
        )

        # Generate occurrence for current period
        _, bill1 = RecurringScheduleService.trigger_occurrence(schedule, self.owner)
        period1 = RecurringOccurrence.objects.filter(schedule=schedule).first().period_identifier

        # Modify schedule (e.g. increase plan price and change due day)
        RecurringScheduleService.update_schedule(
            schedule=schedule,
            user=self.owner,
            expected_amount=Decimal("850.00"),
            day_of_month=25,
        )

        # Attempting generator execution will not re-create period1
        pairs = RecurringScheduleService.generate_due_occurrences(generate_ahead_days=0)
        assert len(pairs) == 0
        assert Bill.objects.filter(provider_name="Cloud Storage Subscription").count() == 1

    def test_celery_generate_recurring_bills_task(self):
        """Celery shared task runs periodic recurring bill generation idempotently."""
        today = timezone.localdate()
        RecurringScheduleService.create_schedule(
            household=self.household,
            created_by=self.owner,
            provider_name="Task Scheduled Bill",
            category=BillCategory.WATER,
            expected_amount=Decimal("500.00"),
            next_due_date=today + timedelta(days=2),
        )

        # Execute task directly
        count = generate_recurring_bills_task(generate_ahead_days=10)
        assert count == 1
        assert Bill.objects.filter(provider_name="Task Scheduled Bill").count() == 1

        # Second execution: 0 generated (idempotent)
        count_repeat = generate_recurring_bills_task(generate_ahead_days=10)
        assert count_repeat == 0

    def test_web_recurring_schedules_list_view_and_summary_metrics(self):
        """GET /recurring/ renders schedules list, metrics, and actions in web UI."""
        today = timezone.localdate()
        RecurringScheduleService.create_schedule(
            household=self.household,
            created_by=self.owner,
            provider_name="Netflix 4K",
            category=BillCategory.OTHER,
            expected_amount=Decimal("649.00"),
            frequency=RecurrenceFrequency.MONTHLY,
            next_due_date=today + timedelta(days=15),
            is_active=True,
        )
        RecurringScheduleService.create_schedule(
            household=self.household,
            created_by=self.owner,
            provider_name="Spotify Family",
            category=BillCategory.OTHER,
            expected_amount=Decimal("179.00"),
            frequency=RecurrenceFrequency.MONTHLY,
            next_due_date=today + timedelta(days=20),
            is_active=False,
        )

        response = self.client.get("/recurring/")
        assert response.status_code == status.HTTP_200_OK
        content = response.content.decode("utf-8")

        assert "Netflix 4K" in content
        assert "Spotify Family" in content
        assert "Active" in content
        assert "Paused" in content
        assert "+ New Recurring Schedule" in content

    def test_web_create_and_edit_recurring_schedule_flow(self):
        """User can submit new recurring schedule and edit existing schedule through web UI."""
        today = timezone.localdate()
        due_str = str(today + timedelta(days=12))

        # 1. Create schedule via POST
        create_payload = {
            "provider_name": "Web Created Internet",
            "category": BillCategory.INTERNET,
            "expected_amount": "999.00",
            "frequency": RecurrenceFrequency.MONTHLY,
            "interval_count": "1",
            "day_of_month": "15",
            "next_due_date": due_str,
            "assigned_to": str(self.member.id),
            "is_active": "on",
            "notes": "Fiber 100Mbps plan",
        }
        res_create = self.client.post("/recurring/create/", create_payload, follow=True)
        assert res_create.status_code == status.HTTP_200_OK

        schedule = RecurringSchedule.objects.filter(provider_name="Web Created Internet").first()
        assert schedule is not None
        assert schedule.expected_amount == Decimal("999.00")
        assert schedule.assigned_to == self.member

        # 2. Edit schedule via POST
        edit_payload = {
            "provider_name": "Web Created Internet Ultra",
            "category": BillCategory.INTERNET,
            "expected_amount": "1499.00",
            "frequency": RecurrenceFrequency.MONTHLY,
            "interval_count": "1",
            "day_of_month": "15",
            "next_due_date": due_str,
            "assigned_to": str(self.member.id),
            "is_active": "on",
            "notes": "Upgraded to 300Mbps",
        }
        res_edit = self.client.post(f"/recurring/{schedule.id}/edit/", edit_payload, follow=True)
        assert res_edit.status_code == status.HTTP_200_OK

        schedule.refresh_from_db()
        assert schedule.provider_name == "Web Created Internet Ultra"
        assert schedule.expected_amount == Decimal("1499.00")

    def test_web_trigger_and_pause_actions(self):
        """User can trigger immediate occurrence and pause/resume from web actions."""
        today = timezone.localdate()
        schedule = RecurringScheduleService.create_schedule(
            household=self.household,
            created_by=self.owner,
            provider_name="Action Tested Utility",
            category=BillCategory.WATER,
            expected_amount=Decimal("450.00"),
            next_due_date=today + timedelta(days=5),
        )

        # 1. Trigger bill occurrence immediately
        res_trig = self.client.post(
            "/recurring/",
            {"action": "trigger", "schedule_id": str(schedule.id)},
            follow=True,
        )
        assert res_trig.status_code == status.HTTP_200_OK
        assert Bill.objects.filter(provider_name="Action Tested Utility").count() == 1

        # 2. Pause schedule
        res_pause = self.client.post(
            "/recurring/",
            {"action": "pause", "schedule_id": str(schedule.id)},
            follow=True,
        )
        assert res_pause.status_code == status.HTTP_200_OK
        schedule.refresh_from_db()
        assert schedule.is_active is False

        # 3. Resume schedule
        res_resume = self.client.post(
            "/recurring/",
            {"action": "resume", "schedule_id": str(schedule.id)},
            follow=True,
        )
        assert res_resume.status_code == status.HTTP_200_OK
        schedule.refresh_from_db()
        assert schedule.is_active is True

    def test_web_recurring_detail_view_renders_occurrences_ledger(self):
        """GET /recurring/{schedule_id}/ renders configuration and occurrence history ledger."""
        today = timezone.localdate()
        schedule = RecurringScheduleService.create_schedule(
            household=self.household,
            created_by=self.owner,
            provider_name="Detail View Test",
            category=BillCategory.OTHER,
            expected_amount=Decimal("800.00"),
            next_due_date=today,
        )
        RecurringScheduleService.trigger_occurrence(schedule, self.owner)

        response = self.client.get(f"/recurring/{schedule.id}/")
        assert response.status_code == status.HTTP_200_OK
        content = response.content.decode("utf-8")

        assert "Detail View Test" in content
        assert "₹800.00" in content
        assert "Generated Bill Occurrences (1)" in content
        assert "View Bill Ledger" in content
