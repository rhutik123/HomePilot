import logging
from datetime import date
from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated

from apps.households.models import Household
from apps.analytics.services.analytics_service import AnalyticsService
from apps.analytics.services.export_csv import CSVReportExporter
from apps.analytics.services.export_pdf import PDFReportExporter

logger = logging.getLogger(__name__)


def parse_date(date_str: str) -> date:
    """Safely parses YYYY-MM-DD date strings."""
    if not date_str:
        return None
    try:
        return date.fromisoformat(date_str.strip())
    except ValueError:
        return None


def get_authorized_household(request):
    """
    Retrieves household from query parameters and enforces strict membership boundary.
    Defaults to the user's first household if none specified.
    """
    hh_id = request.query_params.get("household")
    if hh_id:
        try:
            return Household.objects.get(id=hh_id, memberships__user=request.user)
        except Household.DoesNotExist:
            return None
    return Household.objects.filter(memberships__user=request.user).first()


class AnalyticsOverviewView(APIView):
    """
    Returns executive financial summary, KPIs, and bill settlement metrics for a household.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        household = get_authorized_household(request)
        if not household:
            return Response(
                {"error": "Household not found or you are not an authorized member."},
                status=status.HTTP_404_NOT_FOUND,
            )

        date_from = parse_date(request.query_params.get("date_from"))
        date_to = parse_date(request.query_params.get("date_to"))

        metrics = AnalyticsService.get_overview_metrics(household, date_from, date_to)
        return Response(metrics, status=status.HTTP_200_OK)


class CategoryBreakdownView(APIView):
    """
    Returns spending aggregated by category with percentage distribution.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        household = get_authorized_household(request)
        if not household:
            return Response(
                {"error": "Household not found or you are not an authorized member."},
                status=status.HTTP_404_NOT_FOUND,
            )

        date_from = parse_date(request.query_params.get("date_from"))
        date_to = parse_date(request.query_params.get("date_to"))

        breakdown = AnalyticsService.get_category_metrics(household, date_from, date_to)
        return Response({"household": household.name, "categories": breakdown}, status=status.HTTP_200_OK)


class ProviderBreakdownView(APIView):
    """
    Returns spending aggregated by utility/service provider.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        household = get_authorized_household(request)
        if not household:
            return Response(
                {"error": "Household not found or you are not an authorized member."},
                status=status.HTTP_404_NOT_FOUND,
            )

        date_from = parse_date(request.query_params.get("date_from"))
        date_to = parse_date(request.query_params.get("date_to"))

        providers = AnalyticsService.get_provider_metrics(household, date_from, date_to)
        return Response({"household": household.name, "providers": providers}, status=status.HTTP_200_OK)


class SpendingTrendsView(APIView):
    """
    Returns month-over-month spending history and percentage trends for charting.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        household = get_authorized_household(request)
        if not household:
            return Response(
                {"error": "Household not found or you are not an authorized member."},
                status=status.HTTP_404_NOT_FOUND,
            )

        try:
            months = min(int(request.query_params.get("months", 6)), 24)
        except ValueError:
            months = 6

        trends = AnalyticsService.get_spending_trends(household, months)
        return Response({"household": household.name, "trends": trends}, status=status.HTTP_200_OK)


class CSVExportView(APIView):
    """
    Downloads filtered household expense records in CSV format.
    Supports report types: bills (default), payments, recurring, overdue, upcoming.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        household = get_authorized_household(request)
        if not household:
            return Response(
                {"error": "Household not found or you are not an authorized member."},
                status=status.HTTP_404_NOT_FOUND,
            )

        report_type = request.query_params.get("type", "bills").lower().strip()
        date_from = parse_date(request.query_params.get("date_from"))
        date_to = parse_date(request.query_params.get("date_to"))

        if report_type == "payments":
            return CSVReportExporter.export_payments_csv(household, date_from, date_to)
        elif report_type == "recurring":
            return CSVReportExporter.export_recurring_csv(household)
        elif report_type == "overdue":
            return CSVReportExporter.export_overdue_bills_csv(household)
        elif report_type == "upcoming":
            try:
                days = int(request.query_params.get("days", 30))
            except ValueError:
                days = 30
            return CSVReportExporter.export_upcoming_obligations_csv(household, days=days)
        else:
            category = request.query_params.get("category")
            bill_status = request.query_params.get("status")
            return CSVReportExporter.export_bills_csv(
                household=household,
                date_from=date_from,
                date_to=date_to,
                category=category,
                status=bill_status,
            )


class PDFExportView(APIView):
    """
    Generates and downloads a formal PDF financial statement for the household.
    Supports report types: statement (default), payments.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        household = get_authorized_household(request)
        if not household:
            return Response(
                {"error": "Household not found or you are not an authorized member."},
                status=status.HTTP_404_NOT_FOUND,
            )

        report_type = request.query_params.get("type", "statement").lower().strip()
        date_from = parse_date(request.query_params.get("date_from"))
        date_to = parse_date(request.query_params.get("date_to"))

        if report_type == "payments":
            return PDFReportExporter.export_payments_statement_pdf(
                household=household,
                date_from=date_from,
                date_to=date_to,
            )
        else:
            return PDFReportExporter.export_financial_statement_pdf(
                household=household,
                date_from=date_from,
                date_to=date_to,
            )
