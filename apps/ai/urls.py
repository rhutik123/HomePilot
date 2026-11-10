from django.urls import path
from rest_framework.routers import DefaultRouter
from .views import (
    AIConversationViewSet,
    SpendingInsightViewSet,
    AIUsageRecordListView,
)

app_name = "ai"

router = DefaultRouter()
router.register(r"conversations", AIConversationViewSet, basename="conversations")
router.register(r"insights", SpendingInsightViewSet, basename="insights")

urlpatterns = [
    path("usage/", AIUsageRecordListView.as_view(), name="usage-records"),
] + router.urls
