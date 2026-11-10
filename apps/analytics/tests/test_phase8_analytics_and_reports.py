import csv
import io
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
from apps.payments.models import Payment, PaymentMethod, PaymentStatus
from apps.recurring.models import RecurringSchedule, RecurrenceFrequency
from apps.analytics.services.analytics_service import AnalyticsService
from apps.analytics.services.export_csv import CSVReportExporter
from apps.analytics.services.export_pdf import PDFReportExporter


@pytest.fixture(autouse=True)
def mock_localdate():
    with patch("django.utils.timezone.localdate", return_value=datetime.date(2026, 11, 10)):
        yield datetime.date(2026, 11, 10)


@pytest.fixture
def owner_user(db):
    return User.objects.create_user(
        username="owner_phase8",
        email="owner_phase8@example.com",
        password="Password123!",
        first_name="Alice",
        last_name="Owner",
    )


@pytest.fixture
def member_user(db):
    return User.objects.create_user(
        username="member_phase8",
        email="member_phase8@example.com",
        password="Password123!",
        first_name="Bob",
        last_name="Member",
    )


@pytest.fixture
def outsider_user(db):
    return User.objects.create_user(
        username="outsider_phase8",
        email="outsider_phase8@example.com",
        password="Password123!",
        first_name="Eve",
        last_name="Outsider",
    )


@pytest.fixture
def test_household(owner_user, member_user):
    hh = Household.objects.create(name="Phase 8 Analytics Manor", created_by=owner_user)
    HouseholdMembership.objects.create(household=hh, user=owner_user, role=HouseholdMembership.Role.OWNER)
    HouseholdMembership.objects.create(household=hh, user=member_user, role=HouseholdMembership.Role.MEMBER)
    return hh


@pytest.fixture
def foreign_household(outsider_user):
    hh = Household.objects.create(name="Foreign Manor", created_by=outsider_user)
    HouseholdMembership.objects.create(household=hh, user=outsider_user, role=HouseholdMembership.Role.OWNER)
    return hh


@pytest.fixture
def analytics_data(test_household, foreign_household, owner_user, member_user, outsider_user):
    # Today is fixed to 2026-11-10
    # 1. October bill in test household (Tata Power 2000.00, PAID)
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

    # 2. November bill in test household (Tata Power 2600.00, PARTIALLY_PAID)
    b_nov_elec = Bill.objects.create(
        household=test_household,
        uploaded_by=owner_user,
        assigned_to=member_user,
        provider_name="Tata Power",
        category=BillCategory.ELECTRICITY,
        amount=Decimal("2600.00"),
        total_paid=Decimal("1000.00"),
        outstanding_balance=Decimal("1600.00"),
        status=BillStatus.PARTIALLY_PAID,
        due_date=datetime.date(2026, 11, 15),
    )

    # 3. November bill in test household (Airtel Broadband 1200.00, PENDING)
    b_nov_net = Bill.objects.create(
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

    # 4. Overdue bill in test household (City Water 500.00, OVERDUE)
    b_overdue = Bill.objects.create(
        household=test_household,
        uploaded_by=owner_user,
        assigned_to=member_user,
        provider_name="City Water",
        category=BillCategory.WATER,
        amount=Decimal("500.00"),
        total_paid=Decimal("0.00"),
        outstanding_balance=Decimal("500.00"),
        status=BillStatus.OVERDUE,
        due_date=datetime.date(2026, 9, 25),
    )

    # 5. Payment in test household
    p1 = Payment.objects.create(
        household=test_household,
        bill=b_nov_elec,
        paid_by=member_user,
        amount=Decimal("1000.00"),
        payment_date=datetime.date(2026, 11, 5),
        payment_method=PaymentMethod.UPI,
        transaction_reference="UPI/11223344",
    )

    # 6. Recurring schedule in test household
    sched = RecurringSchedule.objects.create(
        household=test_household,
        created_by=owner_user,
        assigned_to=member_user,
        provider_name="Netflix",
        category=BillCategory.OTHER,
        frequency=RecurrenceFrequency.MONTHLY,
        expected_amount=Decimal("649.00"),
        next_due_date=datetime.date(2026, 12, 1),
        is_active=True,
    )

    # 7. Bill in foreign household (MUST NEVER APPEAR IN TEST HOUSEHOLD REPORTS)
    b_foreign = Bill.objects.create(
        household=foreign_household,
        uploaded_by=outsider_user,
        assigned_to=outsider_user,
        provider_name="Foreign Gas Ltd",
        category=BillCategory.GAS,
        amount=Decimal("9999.00"),
        total_paid=Decimal("0.00"),
        outstanding_balance=Decimal("9999.00"),
        status=BillStatus.PENDING,
        due_date=datetime.date(2026, 11, 15),
    )

    return {
        "b_oct": b_oct,
        "b_nov_elec": b_nov_elec,
        "b_nov_net": b_nov_net,
        "b_overdue": b_overdue,
        "payment": p1,
        "schedule": sched,
        "b_foreign": b_foreign,
    }


@pytest.mark.django_db
class TestPhase8AnalyticsServiceCalculations:
    def test_overview_metrics_calculations(self, test_household, analytics_data):
        """Verifies overview metrics calculate exact totals, settlement rate, and overdue sums."""
        res = AnalyticsService.get_overview_metrics(test_household)

        # 2000 (Oct) + 2600 (Nov) + 1200 (Nov) + 500 (Overdue) = 6300.00
        assert Decimal(res["total_billed"]) == Decimal("6300.00")
        # 2000 + 1000 = 3000.00
        assert Decimal(res["total_paid"]) == Decimal("3000.00")
        # 1600 + 1200 + 500 = 3300.00
        assert Decimal(res["total_outstanding"]) == Decimal("3300.00")
        assert Decimal(res["overdue_amount"]) == Decimal("500.00")
        assert res["bill_counts"]["total"] == 4
        assert res["bill_counts"]["overdue"] == 1
        assert res["settlement_rate_pct"] == round((3000 / 6300) * 100, 2)

    def test_category_metrics_calculations(self, test_household, analytics_data):
        """Verifies category breakdown sums and percentages."""
        categories = AnalyticsService.get_category_metrics(test_household)
        assert len(categories) == 3

        cat_map = {c["category"]: c for c in categories}
        assert BillCategory.ELECTRICITY in cat_map
        # Tata Power 2000 + 2600 = 4600.00
        assert Decimal(cat_map[BillCategory.ELECTRICITY]["total_amount"]) == Decimal("4600.00")
        assert cat_map[BillCategory.ELECTRICITY]["bill_count"] == 2

        assert BillCategory.INTERNET in cat_map
        assert Decimal(cat_map[BillCategory.INTERNET]["total_amount"]) == Decimal("1200.00")

        assert BillCategory.WATER in cat_map
        assert Decimal(cat_map[BillCategory.WATER]["total_amount"]) == Decimal("500.00")

    def test_provider_metrics_calculations(self, test_household, analytics_data):
        """Verifies provider breakdown metrics."""
        providers = AnalyticsService.get_provider_metrics(test_household)
        prov_map = {p["provider_name"]: p for p in providers}

        assert "Tata Power" in prov_map
        assert Decimal(prov_map["Tata Power"]["total_amount"]) == Decimal("4600.00")
        assert Decimal(prov_map["Tata Power"]["total_paid"]) == Decimal("3000.00")
        assert Decimal(prov_map["Tata Power"]["outstanding_balance"]) == Decimal("1600.00")

    def test_spending_trends_calculations(self, test_household, analytics_data):
        """Verifies month-over-month trends."""
        trends = AnalyticsService.get_spending_trends(test_household, months=6)
        assert len(trends) >= 2

        t_map = {t["period"]: t for t in trends}
        assert "2026-10" in t_map
        assert Decimal(t_map["2026-10"]["total_billed"]) == Decimal("2000.00")

        assert "2026-11" in t_map
        # 2600 + 1200 = 3800.00
        assert Decimal(t_map["2026-11"]["total_billed"]) == Decimal("3800.00")

    def test_recurring_and_obligations_metrics(self, test_household, analytics_data):
        """Verifies recurring commitments and upcoming obligations."""
        recurring = AnalyticsService.get_recurring_metrics(test_household)
        assert recurring["active_schedules_count"] == 1
        assert recurring["estimated_monthly_recurring_total"] == "649.00"

        obligations = AnalyticsService.get_obligations_metrics(test_household)
        assert obligations["overdue_count"] == 1
        assert obligations["overdue_total"] == "500.00"
        # Tata Power (due Nov 15, in 5 days) + Airtel (due Nov 20, in 10 days)
        # Next 7 days: Tata Power 1600.00
        assert obligations["due_next_week_count"] == 1
        assert obligations["due_next_week_total"] == "1600.00"
        # Next 30 days: 1600 + 1200 = 2800.00
        assert obligations["due_next_month_count"] == 2
        assert obligations["due_next_month_total"] == "2800.00"


@pytest.mark.django_db
class TestPhase8ExportReports:
    def test_export_bills_csv(self, test_household, analytics_data):
        """Tests bills CSV export format, headers, line items, and totals."""
        response = CSVReportExporter.export_bills_csv(test_household)
        assert response.status_code == 200
        assert response["Content-Type"] == "text/csv"

        content = response.content.decode("utf-8")
        reader = list(csv.reader(io.StringIO(content)))
        headers = reader[0]
        assert "Bill ID" in headers
        assert "Provider Name" in headers
        assert "Outstanding Balance" in headers

        # Foreign bill MUST NOT appear in test household CSV
        assert "Foreign Gas Ltd" not in content
        assert "Tata Power" in content
        assert "Airtel Broadband" in content

    def test_export_payments_csv(self, test_household, analytics_data):
        """Tests payments CSV export format and values."""
        response = CSVReportExporter.export_payments_csv(test_household)
        assert response.status_code == 200
        content = response.content.decode("utf-8")

        assert "Payment ID" in content
        assert "UPI/11223344" in content
        assert "1000.00" in content

    def test_export_recurring_csv(self, test_household, analytics_data):
        """Tests recurring schedules CSV export."""
        response = CSVReportExporter.export_recurring_csv(test_household)
        assert response.status_code == 200
        content = response.content.decode("utf-8")

        assert "Schedule ID" in content
        assert "Netflix" in content
        assert "649.00" in content

    def test_export_overdue_and_upcoming_csv(self, test_household, analytics_data):
        """Tests overdue and upcoming CSV exports."""
        res_overdue = CSVReportExporter.export_overdue_bills_csv(test_household)
        assert res_overdue.status_code == 200
        content_overdue = res_overdue.content.decode("utf-8")
        assert "City Water" in content_overdue
        assert "500.00" in content_overdue

        res_upcoming = CSVReportExporter.export_upcoming_obligations_csv(test_household, days=30)
        assert res_upcoming.status_code == 200
        content_upcoming = res_upcoming.content.decode("utf-8")
        assert "Tata Power" in content_upcoming
        assert "Airtel Broadband" in content_upcoming

    def test_export_financial_statement_pdf(self, test_household, analytics_data):
        """Tests PDF financial statement generation starts with %PDF header."""
        response = PDFReportExporter.export_financial_statement_pdf(test_household)
        assert response.status_code == 200
        assert response["Content-Type"] == "application/pdf"
        assert response.content.startswith(b"%PDF-")
        assert len(response.content) > 1000

    def test_export_payments_statement_pdf(self, test_household, analytics_data):
        """Tests PDF payment statement generation."""
        response = PDFReportExporter.export_payments_statement_pdf(test_household)
        assert response.status_code == 200
        assert response["Content-Type"] == "application/pdf"
        assert response.content.startswith(b"%PDF-")


@pytest.mark.django_db
class TestPhase8SecurityAndWebInterface:
    def test_api_tenant_isolation_foreign_user_cannot_export(self, test_household, outsider_user, analytics_data):
        """Foreign user cannot export or view reports from another household."""
        client = APIClient()
        client.force_authenticate(user=outsider_user)

        # 1. Overview endpoint returns 404
        res_overview = client.get(f"/api/v1/analytics/overview/?household={test_household.id}")
        assert res_overview.status_code == status.HTTP_404_NOT_FOUND

        # 2. CSV export returns 404
        res_csv = client.get(f"/api/v1/analytics/reports/csv/?household={test_household.id}")
        assert res_csv.status_code == status.HTTP_404_NOT_FOUND

        # 3. PDF export returns 404
        res_pdf = client.get(f"/api/v1/analytics/reports/pdf/?household={test_household.id}")
        assert res_pdf.status_code == status.HTTP_404_NOT_FOUND

    def test_api_export_query_types(self, test_household, member_user, analytics_data):
        """Verifies API CSV export with type query parameters."""
        client = APIClient()
        client.force_authenticate(user=member_user)

        # 1. Type: payments
        res_pay = client.get(f"/api/v1/analytics/reports/csv/?household={test_household.id}&type=payments")
        assert res_pay.status_code == status.HTTP_200_OK
        assert "UPI/11223344" in res_pay.content.decode()

        # 2. Type: recurring
        res_rec = client.get(f"/api/v1/analytics/reports/csv/?household={test_household.id}&type=recurring")
        assert res_rec.status_code == status.HTTP_200_OK
        assert "Netflix" in res_rec.content.decode()

        # 3. Type: overdue
        res_od = client.get(f"/api/v1/analytics/reports/csv/?household={test_household.id}&type=overdue")
        assert res_od.status_code == status.HTTP_200_OK
        assert "City Water" in res_od.content.decode()

    def test_web_reports_view_and_export_download(self, test_household, member_user, analytics_data):
        """Tests the web UI reports dashboard and download endpoints."""
        client = APIClient()
        client.force_login(member_user)

        # 1. Load reports web view
        res = client.get(f"/reports/?household={test_household.id}")
        assert res.status_code == status.HTTP_200_OK
        content = res.content.decode()
        assert "Household Reports & Analytics" in content
        assert "Tata Power" in content
        assert "Download PDF Statement" in content

        # 2. Web PDF export
        res_pdf = client.get(f"/reports/export/?household={test_household.id}&format=pdf&type=statement")
        assert res_pdf.status_code == status.HTTP_200_OK
        assert res_pdf["Content-Type"] == "application/pdf"
        assert res_pdf.content.startswith(b"%PDF-")

        # 3. Web CSV export
        res_csv = client.get(f"/reports/export/?household={test_household.id}&format=csv&type=bills")
        assert res_csv.status_code == status.HTTP_200_OK
        assert "Tata Power" in res_csv.content.decode()
