from django.urls import path
from rest_framework.routers import DefaultRouter
from .views import RecurringScheduleViewSet

app_name = "recurring"

router = DefaultRouter()
router.register(r"", RecurringScheduleViewSet, basename="recurring")

urlpatterns = router.urls
