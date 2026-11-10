from django.urls import path
from rest_framework.routers import DefaultRouter
from .views import (
    BillReminderViewSet,
    NotificationLogListView,
    NotificationPreferenceView,
    InAppNotificationViewSet,
    NotificationDeliveryListView,
)

app_name = "reminders"

router = DefaultRouter()
router.register(r"inbox", InAppNotificationViewSet, basename="inbox")
router.register(r"", BillReminderViewSet, basename="reminder")

urlpatterns = [
    path("preferences/", NotificationPreferenceView.as_view(), name="preferences"),
    path("deliveries/", NotificationDeliveryListView.as_view(), name="deliveries"),
    path("logs/", NotificationLogListView.as_view(), name="notification-logs"),
] + router.urls

