from django.urls import path
from .views import DashboardSummaryAPIView

app_name = "dashboard_api"

urlpatterns = [
    path("summary/", DashboardSummaryAPIView.as_view(), name="summary"),
]
