from datetime import timedelta
from decimal import Decimal
import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework import status

from apps.households.models import Household, HouseholdMembership
from apps.bills.models import Bill, BillStatus

User = get_user_model()


@pytest.mark.django_db
class TestDashboard:
    def setup_method(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="dash_user",
            email="dash_user@example.com",
            password="Password123!",
            first_name="Dash",
            last_name="Tester",
        )
        self.household = Household.objects.create(name="Dashboard Family", created_by=self.user)
        HouseholdMembership.objects.create(household=self.household, user=self.user, role=HouseholdMembership.Role.OWNER)

        today = timezone.localdate()

        # 1. Pending bill
        self.pending_bill = Bill.objects.create(
            household=self.household,
            uploaded_by=self.user,
            assigned_to=self.user,
            provider_name="Pending Power",
            amount=Decimal("1200.00"),
            due_date=today + timedelta(days=5),
            status=BillStatus.PENDING,
        )

        # 2. Overdue bill
        self.overdue_bill = Bill.objects.create(
            household=self.household,
            uploaded_by=self.user,
            assigned_to=self.user,
            provider_name="Overdue Water",
            amount=Decimal("450.00"),
            due_date=today - timedelta(days=3),
            status=BillStatus.PENDING,
        )

        # 3. Paid bill
        self.paid_bill = Bill.objects.create(
            household=self.household,
            uploaded_by=self.user,
            provider_name="Paid Internet",
            amount=Decimal("899.00"),
            due_date=today + timedelta(days=2),
            status=BillStatus.PAID,
            paid_at=timezone.now(),
        )

    def test_dashboard_summary_api_metrics(self):
        """Dashboard API aggregates counts and amounts accurately."""
        self.client.force_authenticate(user=self.user)
        response = self.client.get("/api/dashboard/summary/")
        assert response.status_code == status.HTTP_200_OK

        metrics = response.data["metrics"]
        assert metrics["pending_count"] == 1
        assert metrics["pending_total"] == "1200.00"
        assert metrics["overdue_count"] == 1
        assert metrics["overdue_total"] == "450.00"
        assert metrics["paid_count"] == 1
        assert metrics["paid_total"] == "899.00"
        assert metrics["assigned_to_me_count"] == 2  # pending + overdue assigned to me

    def test_web_dashboard_authenticated(self):
        """Authenticated web user can view dashboard HTML."""
        self.client.force_login(self.user)
        response = self.client.get("/dashboard/")
        assert response.status_code == status.HTTP_200_OK
        assert b"Family Bills Dashboard" in response.content

    def test_web_dashboard_unauthenticated_redirects(self):
        """Unauthenticated user is redirected to login."""
        response = self.client.get("/dashboard/")
        assert response.status_code == status.HTTP_302_FOUND
        assert "/login/" in response.url

    def test_web_bills_list_view(self):
        """Bills list view loads with bills displayed."""
        self.client.force_login(self.user)
        response = self.client.get("/bills/")
        assert response.status_code == status.HTTP_200_OK
        assert b"Pending Power" in response.content

    def test_web_mark_bill_paid_action(self):
        """Clicking mark-paid from web UI updates bill and redirects."""
        self.client.force_login(self.user)
        response = self.client.get(f"/bills/{self.pending_bill.id}/mark-paid/")
        assert response.status_code == status.HTTP_302_FOUND

        self.pending_bill.refresh_from_db()
        assert self.pending_bill.status == BillStatus.PAID
        assert self.pending_bill.paid_at is not None
