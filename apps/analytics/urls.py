from django.urls import path
from .views import (
    AnalyticsOverviewView,
    CategoryBreakdownView,
    ProviderBreakdownView,
    SpendingTrendsView,
    CSVExportView,
    PDFExportView,
)

app_name = "analytics"

urlpatterns = [
    path("overview/", AnalyticsOverviewView.as_view(), name="overview"),
    path("categories/", CategoryBreakdownView.as_view(), name="categories"),
    path("providers/", ProviderBreakdownView.as_view(), name="providers"),
    path("trends/", SpendingTrendsView.as_view(), name="trends"),
    path("reports/csv/", CSVExportView.as_view(), name="export-csv"),
    path("reports/pdf/", PDFExportView.as_view(), name="export-pdf"),
]
