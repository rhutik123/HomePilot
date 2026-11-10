from django.urls import path
from .views import AuditEventListAPIView

app_name = "audit"

urlpatterns = [
    path("", AuditEventListAPIView.as_view(), name="audit_event_list"),
]
