from django.urls import path
from rest_framework.routers import DefaultRouter
from .views import BillReminderViewSet, NotificationLogListView

app_name = "reminders"

router = DefaultRouter()
router.register(r"", BillReminderViewSet, basename="reminder")

urlpatterns = [
    path("logs/", NotificationLogListView.as_view(), name="notification-logs"),
] + router.urls
