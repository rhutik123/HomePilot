from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (
    BillViewSet,
    BillDocumentUploadAPIView,
    BillDocumentDetailAPIView,
    BillDocumentDownloadAPIView,
)

app_name = "bills"

router = DefaultRouter()
router.register(r"", BillViewSet, basename="bill")

urlpatterns = [
    path("documents/upload/", BillDocumentUploadAPIView.as_view(), name="document-upload"),
    path("documents/<int:pk>/", BillDocumentDetailAPIView.as_view(), name="document-detail"),
    path("documents/<int:pk>/download/", BillDocumentDownloadAPIView.as_view(), name="document-download"),
] + router.urls
