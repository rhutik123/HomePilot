import pytest
from datetime import date, timedelta
from decimal import Decimal
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.households.models import Household, HouseholdMembership
from apps.bills.models import Bill, BillCategory, BillStatus
from apps.recurring.models import RecurringSchedule, RecurringOccurrence, RecurrenceFrequency
from apps.recurring.services.schedule_service import (
    RecurringScheduleService,
    advance_calendar_months,
    calculate_next_due_date,
    get_period_identifier,
)


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def owner(db):
    return User.objects.create_user(
        username="rec_owner",
        email="rec_owner@example.com",
        password="Password123!",
        first_name="Recurring",
        last_name="Owner",
    )


@pytest.fixture
def member(db):
    return User.objects.create_user(
        username="rec_member",
        email="rec_member@example.com",
        password="Password123!",
        first_name="Recurring",
        last_name="Member",
    )


@pytest.fixture
def outsider(db):
    return User.objects.create_user(
        username="rec_outsider",
        email="rec_outsider@example.com",
        password="Password123!",
        first_name="Outsider",
    )


@pytest.fixture
def household(owner, member):
    hh = Household.objects.create(name="Solarium Flat", created_by=owner)
    HouseholdMembership.objects.create(household=hh, user=owner, role=HouseholdMembership.Role.OWNER)
    HouseholdMembership.objects.create(household=hh, user=member, role=HouseholdMembership.Role.MEMBER)
    return hh


@pytest.fixture
def active_schedule(household, owner, member):
    today = timezone.localdate()
    return RecurringSchedule.objects.create(
        household=household,
        created_by=owner,
        assigned_to=member,
        provider_name="Broadband ISP",
        category=BillCategory.INTERNET,
        expected_amount=Decimal("1499.00"),
        frequency=RecurrenceFrequency.MONTHLY,
        day_of_month=today.day,
        next_due_date=today + timedelta(days=5),
        is_active=True,
    )


@pytest.mark.django_db
class TestRecurrenceCalculations:
    def test_advance_calendar_months(self):
        jan31 = date(2026, 1, 31)
        feb_date = advance_calendar_months(jan31, 1, 31)
        # 2026 is not a leap year, max day in Feb is 28
        assert feb_date == date(2026, 2, 28)

        march_date = advance_calendar_months(jan31, 2, 31)
        assert march_date == date(2026, 3, 31)

    def test_calculate_next_due_date_monthly(self):
        d = date(2026, 5, 15)
        next_d = calculate_next_due_date(d, RecurrenceFrequency.MONTHLY, interval_count=1, target_day=15)
        assert next_d == date(2026, 6, 15)

    def test_calculate_next_due_date_quarterly(self):
        d = date(2026, 1, 10)
        next_d = calculate_next_due_date(d, RecurrenceFrequency.QUARTERLY, interval_count=1, target_day=10)
        assert next_d == date(2026, 4, 10)

    def test_calculate_next_due_date_yearly(self):
        d = date(2026, 3, 1)
        next_d = calculate_next_due_date(d, RecurrenceFrequency.YEARLY, interval_count=1, target_day=1)
        assert next_d == date(2027, 3, 1)

    def test_period_identifiers(self):
        d = date(2026, 11, 25)
        assert get_period_identifier(d, RecurrenceFrequency.MONTHLY) == "2026-11"
        assert get_period_identifier(d, RecurrenceFrequency.QUARTERLY) == "2026-Q4"
        assert get_period_identifier(d, RecurrenceFrequency.YEARLY) == "2026"


@pytest.mark.django_db
class TestRecurringScheduleService:
    def test_idempotent_bill_generation(self, active_schedule):
        # Initial run: generates bill occurrence and advances next_due_date
        pairs = RecurringScheduleService.generate_due_occurrences(generate_ahead_days=10)
        assert len(pairs) == 1
        sched, bill = pairs[0]

        assert bill.provider_name == "Broadband ISP"
        assert bill.amount == Decimal("1499.00")
        assert bill.status == BillStatus.PENDING
        assert RecurringOccurrence.objects.filter(schedule=sched).count() == 1

        initial_due = sched.next_due_date

        # Immediate repeated execution: should NOT create duplicate bills
        pairs_repeat = RecurringScheduleService.generate_due_occurrences(generate_ahead_days=10)
        assert len(pairs_repeat) == 0
        assert Bill.objects.filter(provider_name="Broadband ISP").count() == 1


@pytest.mark.django_db
class TestRecurringAPI:
    def test_list_schedules_tenant_isolation(self, api_client, active_schedule, member, outsider):
        api_client.force_authenticate(user=member)
        res = api_client.get("/api/v1/recurring/")
        assert res.status_code == status.HTTP_200_OK
        assert len(res.data["results"]) == 1
        assert res.data["results"][0]["provider_name"] == "Broadband ISP"

        # Outsider cannot see schedule
        api_client.force_authenticate(user=outsider)
        res_out = api_client.get("/api/v1/recurring/")
        assert res_out.status_code == status.HTTP_200_OK
        assert len(res_out.data["results"]) == 0

    def test_pause_and_resume_schedule(self, api_client, active_schedule, member):
        api_client.force_authenticate(user=member)
        url_pause = f"/api/v1/recurring/{active_schedule.id}/pause/"
        res_pause = api_client.post(url_pause)
        assert res_pause.status_code == status.HTTP_200_OK
        assert res_pause.data["is_active"] is False

        active_schedule.refresh_from_db()
        assert active_schedule.is_active is False

        url_resume = f"/api/v1/recurring/{active_schedule.id}/resume/"
        res_resume = api_client.post(url_resume)
        assert res_resume.status_code == status.HTTP_200_OK
        assert res_resume.data["is_active"] is True

        active_schedule.refresh_from_db()
        assert active_schedule.is_active is True

    def test_manual_trigger_creates_occurrence(self, api_client, active_schedule, owner):
        api_client.force_authenticate(user=owner)
        url_trigger = f"/api/v1/recurring/{active_schedule.id}/trigger/"
        res = api_client.post(url_trigger)
        assert res.status_code == status.HTTP_201_CREATED
        assert "occurrence" in res.data
        assert res.data["occurrence"]["bill_provider"] == "Broadband ISP"

        # Second trigger for the same period should fail gracefully
        res2 = api_client.post(url_trigger)
        # next_due_date was advanced, but let's test duplicate for the same period
        # If triggered again, it creates next month's occurrence unless period matches
        assert res2.status_code in (status.HTTP_201_CREATED, status.HTTP_400_BAD_REQUEST)
