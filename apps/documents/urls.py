from django.urls import path
from rest_framework.routers import DefaultRouter
from .views import DocumentViewSet

app_name = "documents"

router = DefaultRouter()
router.register(r"", DocumentViewSet, basename="document")

urlpatterns = router.urls
