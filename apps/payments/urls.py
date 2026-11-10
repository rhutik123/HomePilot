from django.urls import path
from rest_framework.routers import DefaultRouter
from .views import PaymentViewSet, BillPaymentListCreateAPIView

app_name = "payments"

router = DefaultRouter()
router.register(r"", PaymentViewSet, basename="payment")

urlpatterns = [
    path("bills/<int:bill_id>/payments/", BillPaymentListCreateAPIView.as_view(), name="bill-payments"),
] + router.urls
