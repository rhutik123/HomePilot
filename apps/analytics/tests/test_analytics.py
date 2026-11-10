import pytest
import csv
import io
from decimal import Decimal
from datetime import timedelta
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.households.models import Household, HouseholdMembership
from apps.bills.models import Bill, BillCategory, BillStatus
from apps.payments.models import Payment, PaymentMethod
from apps.analytics.services.analytics_service import AnalyticsService
from apps.analytics.services.export_csv import CSVReportExporter
from apps.analytics.services.export_pdf import PDFReportExporter


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def owner(db):
    return User.objects.create_user(
        username="analytics_owner",
        email="analytics_owner@example.com",
        password="Password123!",
        first_name="Analytics",
        last_name="Owner",
    )


@pytest.fixture
def member(db):
    return User.objects.create_user(
        username="analytics_member",
        email="analytics_member@example.com",
        password="Password123!",
        first_name="Analytics",
        last_name="Member",
    )


@pytest.fixture
def outsider(db):
    return User.objects.create_user(
        username="analytics_outsider",
        email="analytics_outsider@example.com",
        password="Password123!",
        first_name="Analytics",
        last_name="Outsider",
    )


@pytest.fixture
def household(owner, member):
    hh = Household.objects.create(name="Skyline Heights", created_by=owner)
    HouseholdMembership.objects.create(household=hh, user=owner, role=HouseholdMembership.Role.OWNER)
    HouseholdMembership.objects.create(household=hh, user=member, role=HouseholdMembership.Role.MEMBER)
    return hh


@pytest.fixture
def foreign_household(outsider):
    hh = Household.objects.create(name="Remote Valley", created_by=outsider)
    HouseholdMembership.objects.create(household=hh, user=outsider, role=HouseholdMembership.Role.OWNER)
    return hh


@pytest.fixture
def sample_dataset(household, owner, member):
    today = timezone.localdate()

    # Bill 1: Electricity, Paid in full (2000 INR)
    b1 = Bill.objects.create(
        household=household,
        uploaded_by=owner,
        assigned_to=member,
        provider_name="City Power",
        category=BillCategory.ELECTRICITY,
        amount=Decimal("2000.00"),
        total_paid=Decimal("2000.00"),
        outstanding_balance=Decimal("0.00"),
        status=BillStatus.PAID,
        due_date=today - timedelta(days=20),
    )
    Payment.objects.create(
        bill=b1,
        household=household,
        paid_by=member,
        amount=Decimal("2000.00"),
        payment_date=today - timedelta(days=21),
        payment_method=PaymentMethod.UPI,
    )

    # Bill 2: Water, Partially Paid (1000 billed, 400 paid, 600 due)
    b2 = Bill.objects.create(
        household=household,
        uploaded_by=owner,
        assigned_to=owner,
        provider_name="Municipal Water",
        category=BillCategory.WATER,
        amount=Decimal("1000.00"),
        total_paid=Decimal("400.00"),
        outstanding_balance=Decimal("600.00"),
        status=BillStatus.PARTIALLY_PAID,
        due_date=today - timedelta(days=5),
    )
    Payment.objects.create(
        bill=b2,
        household=household,
        paid_by=owner,
        amount=Decimal("400.00"),
        payment_date=today - timedelta(days=4),
        payment_method=PaymentMethod.NET_BANKING,
    )

    # Bill 3: Internet, Overdue (1500 billed, 0 paid)
    b3 = Bill.objects.create(
        household=household,
        uploaded_by=member,
        assigned_to=member,
        provider_name="FiberNet Pro",
        category=BillCategory.INTERNET,
        amount=Decimal("1500.00"),
        total_paid=Decimal("0.00"),
        outstanding_balance=Decimal("1500.00"),
        status=BillStatus.OVERDUE,
        due_date=today - timedelta(days=10),
    )

    # Bill 4: Gas, Pending (25000 billed, 0 paid, due in 5 days)
    b4 = Bill.objects.create(
        household=household,
        uploaded_by=owner,
        assigned_to=owner,
        provider_name="City Gas Pipeline",
        category=BillCategory.GAS,
        amount=Decimal("25000.00"),
        total_paid=Decimal("0.00"),
        outstanding_balance=Decimal("25000.00"),
        status=BillStatus.PENDING,
        due_date=today + timedelta(days=5),
    )

    return [b1, b2, b3, b4]


@pytest.mark.django_db
class TestAnalyticsService:
    def test_overview_metrics_calculations(self, household, sample_dataset):
        metrics = AnalyticsService.get_overview_metrics(household)

        # Total billed = 2000 + 1000 + 1500 + 25000 = 29500.00
        assert Decimal(metrics["total_billed"]) == Decimal("29500.00")
        # Total paid = 2000 + 400 = 2400.00
        assert Decimal(metrics["total_paid"]) == Decimal("2400.00")
        # Total outstanding = 29500 - 2400 = 27100.00
        assert Decimal(metrics["total_outstanding"]) == Decimal("27100.00")

        # Settlement rate: 2400 / 29500 * 100 = 8.14%
        assert round(metrics["settlement_rate_pct"], 2) == 8.14

        # Counts
        assert metrics["bill_counts"]["total"] == 4
        assert metrics["bill_counts"]["paid"] == 1
        assert metrics["bill_counts"]["partially_paid"] == 1
        assert metrics["bill_counts"]["pending"] == 1
        assert metrics["bill_counts"]["overdue"] == 1
        assert Decimal(metrics["overdue_amount"]) == Decimal("1500.00")

        # Payments recorded
        assert metrics["payments_recorded"]["count"] == 2
        assert Decimal(metrics["payments_recorded"]["total_amount"]) == Decimal("2400.00")

    def test_category_metrics_breakdown(self, household, sample_dataset):
        cat_metrics = AnalyticsService.get_category_metrics(household)
        assert len(cat_metrics) == 4

        # GAS is highest at 25000
        gas_cat = cat_metrics[0]
        assert gas_cat["category"] == BillCategory.GAS
        assert Decimal(gas_cat["total_amount"]) == Decimal("25000.00")
        assert gas_cat["percentage_of_total"] > 80.0

    def test_provider_metrics_breakdown(self, household, sample_dataset):
        prov_metrics = AnalyticsService.get_provider_metrics(household)
        assert len(prov_metrics) == 4
        assert prov_metrics[0]["provider_name"] == "City Gas Pipeline"

    def test_spending_trends(self, household, sample_dataset):
        trends = AnalyticsService.get_spending_trends(household, months=3)
        assert len(trends) >= 1
        current_trend = trends[-1]
        assert Decimal(current_trend["total_billed"]) > Decimal("0.00")


@pytest.mark.django_db
class TestCSVReportExporter:
    def test_export_bills_csv(self, household, sample_dataset):
        response = CSVReportExporter.export_bills_csv(household)
        assert response.status_code == 200
        assert response["Content-Type"] == "text/csv"
        assert "attachment; filename=" in response["Content-Disposition"]

        # Parse CSV content
        content = response.content.decode("utf-8")
        reader = csv.reader(io.StringIO(content))
        rows = list(reader)

        header = rows[0]
        assert "Bill ID" in header
        assert "Provider Name" in header
        assert "Amount" in header
        assert "Outstanding Balance" in header

        # Data rows + blank row + totals row
        data_rows = [r for r in rows[1:] if r and r[0] != "TOTALS"]
        assert len(data_rows) == 4

        # Totals row check
        totals_row = next(r for r in rows if r and r[0] == "TOTALS")
        assert "29500.00" in totals_row[3]
        assert "2400.00" in totals_row[5]

    def test_export_bills_csv_with_filter(self, household, sample_dataset):
        response = CSVReportExporter.export_bills_csv(household, category=BillCategory.ELECTRICITY)
        content = response.content.decode("utf-8")
        reader = csv.reader(io.StringIO(content))
        data_rows = [r for r in list(reader)[1:] if r and r[0] != "TOTALS"]
        assert len(data_rows) == 1
        assert "City Power" in data_rows[0][1]


@pytest.mark.django_db
class TestPDFReportExporter:
    def test_export_financial_statement_pdf(self, household, sample_dataset):
        response = PDFReportExporter.export_financial_statement_pdf(household)
        assert response.status_code == 200
        assert response["Content-Type"] == "application/pdf"
        assert "attachment; filename=" in response["Content-Disposition"]

        # Valid PDF header signature
        pdf_bytes = response.content
        assert pdf_bytes.startswith(b"%PDF-")
        assert len(pdf_bytes) > 1000  # Substantial PDF document generated


@pytest.mark.django_db
class TestAnalyticsAPI:
    def test_overview_endpoint(self, api_client, household, member, sample_dataset):
        api_client.force_authenticate(user=member)
        url = f"/api/v1/analytics/overview/?household={household.id}"
        res = api_client.get(url)
        assert res.status_code == status.HTTP_200_OK
        assert Decimal(res.data["total_billed"]) == Decimal("29500.00")
        assert res.data["bill_counts"]["total"] == 4

    def test_categories_endpoint(self, api_client, household, member, sample_dataset):
        api_client.force_authenticate(user=member)
        url = f"/api/v1/analytics/categories/?household={household.id}"
        res = api_client.get(url)
        assert res.status_code == status.HTTP_200_OK
        assert len(res.data["categories"]) == 4

    def test_trends_endpoint(self, api_client, household, member, sample_dataset):
        api_client.force_authenticate(user=member)
        url = f"/api/v1/analytics/trends/?household={household.id}&months=6"
        res = api_client.get(url)
        assert res.status_code == status.HTTP_200_OK
        assert "trends" in res.data

    def test_csv_export_endpoint(self, api_client, household, member, sample_dataset):
        api_client.force_authenticate(user=member)
        url = f"/api/v1/analytics/reports/csv/?household={household.id}"
        res = api_client.get(url)
        assert res.status_code == status.HTTP_200_OK
        assert res["Content-Type"] == "text/csv"

    def test_pdf_export_endpoint(self, api_client, household, member, sample_dataset):
        api_client.force_authenticate(user=member)
        url = f"/api/v1/analytics/reports/pdf/?household={household.id}"
        res = api_client.get(url)
        assert res.status_code == status.HTTP_200_OK
        assert res["Content-Type"] == "application/pdf"
        assert res.content.startswith(b"%PDF-")

    def test_tenant_isolation_outsider_cannot_access_analytics(self, api_client, household, outsider):
        api_client.force_authenticate(user=outsider)
        url = f"/api/v1/analytics/overview/?household={household.id}"
        res = api_client.get(url)
        assert res.status_code == status.HTTP_404_NOT_FOUND
